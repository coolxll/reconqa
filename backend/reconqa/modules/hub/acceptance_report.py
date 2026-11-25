"""Standalone Acceptance Report builder & Markdown / XSS-safe HTML renderers."""

from __future__ import annotations

import html
import json
import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

_DEFENSE_SLOTS: tuple[str, ...] = (
    "happy_path",
    "invalid_params",
    "illegal_state",
    "authz_role",
    "consistency_idempotency",
)

_SLOT_LABELS: dict[str, str] = {
    "happy_path": "Happy Path",
    "invalid_params": "Invalid Params",
    "illegal_state": "Illegal State",
    "authz_role": "AuthZ / Role",
    "consistency_idempotency": "Consistency / Idempotency",
}

_SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}


class AcceptanceApiPrefixItem(BaseModel):
    prefix: str
    total: int = 0
    covered: int = 0
    coverage_pct: float = 0.0


class AcceptanceApiCoverage(BaseModel):
    total_endpoints: int = 0
    har_live_count: int = 0
    js_static_count: int = 0
    coverage_pct: float = 0.0
    runs_merged_count: int = 0
    explore_sessions_count: int = 0
    by_prefix: list[AcceptanceApiPrefixItem] = Field(default_factory=list)


class AcceptanceDefenseSlot(BaseModel):
    slot: str
    status: str = "missing"
    count: int = 0


class AcceptanceCapabilityItem(BaseModel):
    key: str
    module: str = ""
    resource: str = ""
    display_name: str = ""
    title_zh: str = ""
    title_en: str = ""
    recommended_priority: str = "P2"
    is_core: bool = False
    total_endpoints: int = 0
    live_endpoints: int = 0
    live_pct: float = 0.0
    roles: list[str] = Field(default_factory=list)
    case_coverage_pct: float = 0.0
    case_matrix: list[AcceptanceDefenseSlot] = Field(default_factory=list)


class AcceptanceCapabilityMap(BaseModel):
    metrics: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[AcceptanceCapabilityItem] = Field(default_factory=list)


class AcceptanceScenarioAssertionItem(BaseModel):
    id: str = ""
    title: str = ""
    target_endpoint: str = ""
    operator: str = "not_empty"
    expected: str = ""
    status: str = "pending"


class AcceptanceScenarioItem(BaseModel):
    id: str = ""
    slug: str
    name: str
    priority: str = "P2"
    status: str = "active"
    effectiveness_level: str = "L0"
    endpoint_count: int = 0
    run_count: int = 0
    assertion_count: int = 0
    assertions_passed_count: int = 0
    negative_case_count: int = 0
    negative_cases_covered_count: int = 0
    assertions: list[AcceptanceScenarioAssertionItem] = Field(default_factory=list)


class AcceptanceScenarioStats(BaseModel):
    total: int = 0
    by_priority: dict[str, int] = Field(
        default_factory=lambda: {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    )
    by_effectiveness: dict[str, int] = Field(
        default_factory=lambda: {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0}
    )
    p0_p1_total: int = 0
    p0_p1_effective_count: int = 0
    p0_p1_effective_pct: float = 0.0
    total_assertions: int = 0
    assertions_passed: int = 0
    assertions_failed: int = 0
    assertions_pending: int = 0
    total_negative_cases: int = 0
    negative_cases_covered: int = 0
    scenarios: list[AcceptanceScenarioItem] = Field(default_factory=list)


class AcceptanceChainAssertionEvidence(BaseModel):
    id: str = ""
    title: str = ""
    operator: str = ""
    passed: bool = False
    actual: Any = None
    expected: str = ""
    message: str = ""


class AcceptanceChainStepEvidence(BaseModel):
    step_index: int = 0
    step: int = 1
    name: str = ""
    method: str = "GET"
    url: str = ""
    status: str = "skipped"
    status_code: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    assertions: list[AcceptanceChainAssertionEvidence] = Field(default_factory=list)


class AcceptanceChainEvidenceItem(BaseModel):
    chain_run_id: str
    chain_name: str
    run_id: str | None = None
    status: str = "failed"
    total_steps: int = 0
    completed_steps: int = 0
    created_at: str = ""
    updated_at: str = ""
    step_results: list[AcceptanceChainStepEvidence] = Field(default_factory=list)


class AcceptanceChainEvidenceSummary(BaseModel):
    total_chain_runs: int = 0
    passed_chains: int = 0
    failed_chains: int = 0
    chain_pass_rate_pct: float = 0.0
    total_assertions_evaluated: int = 0
    assertions_passed: int = 0
    assertions_failed: int = 0
    chains: list[AcceptanceChainEvidenceItem] = Field(default_factory=list)


class AcceptanceFindingItem(BaseModel):
    id: str = ""
    category: str = ""
    severity: str = "info"
    endpoint: str = ""
    title: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class AcceptanceSecurityFindings(BaseModel):
    total_count: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    high_critical_count: int = 0
    high_critical_items: list[AcceptanceFindingItem] = Field(default_factory=list)


class AcceptancePerformanceSummary(BaseModel):
    total_count: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    high_critical_count: int = 0
    high_critical_items: list[AcceptanceFindingItem] = Field(default_factory=list)
    har_perf_summary: dict[str, Any] | None = None
    slowest_ttfb: list[dict[str, Any]] = Field(default_factory=list)
    k6_analysis: dict[str, Any] | None = None


class AcceptanceSummaryVerdict(BaseModel):
    overall_status: Literal["passed", "attention_needed", "failed"] = "passed"
    api_coverage_pct: float = 0.0
    capability_coverage_pct: float = 0.0
    p0_p1_effective_pct: float = 0.0
    chain_pass_rate_pct: float = 0.0
    high_critical_security_count: int = 0
    high_critical_perf_count: int = 0


class AcceptanceReportData(BaseModel):
    project_id: str
    project_name: str
    project_slug: str = ""
    run_id: str | None = None
    generated_at: str
    summary: AcceptanceSummaryVerdict = Field(default_factory=AcceptanceSummaryVerdict)
    api_coverage: AcceptanceApiCoverage = Field(default_factory=AcceptanceApiCoverage)
    capability_map: AcceptanceCapabilityMap = Field(default_factory=AcceptanceCapabilityMap)
    scenarios: AcceptanceScenarioStats = Field(default_factory=AcceptanceScenarioStats)
    chain_evidence: AcceptanceChainEvidenceSummary = Field(
        default_factory=AcceptanceChainEvidenceSummary
    )
    security_findings: AcceptanceSecurityFindings = Field(
        default_factory=AcceptanceSecurityFindings
    )
    performance: AcceptancePerformanceSummary = Field(default_factory=AcceptancePerformanceSummary)


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    try:
        num = float(str(val).strip()) if isinstance(val, str) else float(val)
        if not math.isfinite(num):
            return default
        return int(num)
    except (ValueError, TypeError, OverflowError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        num = float(str(val).strip()) if isinstance(val, str) else float(val)
        return num if math.isfinite(num) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _normalize_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if isinstance(raw, (list, tuple, set)):
        out: list[str] = []
        for item in raw:
            s = str(item).strip() if item is not None else ""
            if s:
                out.append(s)
        return out
    s = str(raw).strip()
    return [s] if s else []


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return dict(obj)
    out: dict[str, Any] = {}
    for attr in (
        "id",
        "category",
        "severity",
        "endpoint",
        "title",
        "evidence",
        "slug",
        "name",
        "priority",
        "status",
        "effectiveness_level",
        "endpoint_count",
        "run_count",
        "assertion_count",
        "assertions_passed_count",
        "negative_case_count",
        "negative_cases_covered_count",
        "assertions",
    ):
        if hasattr(obj, attr):
            out[attr] = getattr(obj, attr)
    return out


def _is_perf_category(category: str) -> bool:
    low = str(category or "").strip().lower()
    return low.startswith("perf_") or low == "performance"


def _normalize_finding(raw: Any) -> AcceptanceFindingItem:
    d = _to_dict(raw)
    sev = str(d.get("severity") or "info").strip().lower()
    ev = d.get("evidence")
    return AcceptanceFindingItem(
        id=str(d.get("id") or ""),
        category=str(d.get("category") or ""),
        severity=sev,
        endpoint=str(d.get("endpoint") or ""),
        title=str(d.get("title") or ""),
        evidence=dict(ev) if isinstance(ev, dict) else {},
    )


def _normalize_defense_slots(raw_matrix: Any) -> list[AcceptanceDefenseSlot]:
    by_slot: dict[str, AcceptanceDefenseSlot] = {}
    if isinstance(raw_matrix, list):
        for item in raw_matrix:
            if not isinstance(item, dict):
                continue
            slot_name = str(item.get("slot") or "").strip()
            if not slot_name:
                continue
            by_slot[slot_name] = AcceptanceDefenseSlot(
                slot=slot_name,
                status=str(item.get("status") or "missing").strip().lower(),
                count=_safe_int(item.get("count"), 0),
            )
    ordered: list[AcceptanceDefenseSlot] = []
    for slot_name in _DEFENSE_SLOTS:
        ordered.append(
            by_slot.get(slot_name, AcceptanceDefenseSlot(slot=slot_name, status="missing", count=0))
        )
    return ordered


def build_acceptance_report_data(
    *,
    project_id: str,
    project_name: str,
    project_slug: str = "",
    run_id: str | None = None,
    inventory: dict[str, Any] | None = None,
    capability_map: dict[str, Any] | None = None,
    scenario_list: Any = None,
    scenarios: list[Any] | None = None,
    chain_runs: list[Any] | None = None,
    findings: list[Any] | None = None,
    har_perf_report: dict[str, Any] | None = None,
    k6_analysis: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> AcceptanceReportData:
    """Aggregate all 5 acceptance pillars into a typed AcceptanceReportData payload."""
    inv = inventory if isinstance(inventory, dict) else {}
    raw_by_prefix = inv.get("by_prefix")
    by_prefix_items = [
        AcceptanceApiPrefixItem(
            prefix=str(p.get("prefix") or "/"),
            total=_safe_int(p.get("total"), 0),
            covered=_safe_int(p.get("covered"), 0),
            coverage_pct=_safe_float(p.get("coverage_pct"), 0.0),
        )
        for p in (raw_by_prefix if isinstance(raw_by_prefix, list) else [])
        if isinstance(p, dict)
    ]
    api_coverage = AcceptanceApiCoverage(
        total_endpoints=_safe_int(inv.get("total_endpoints"), 0),
        har_live_count=_safe_int(inv.get("har_live_count"), 0),
        js_static_count=_safe_int(inv.get("js_static_count"), 0),
        coverage_pct=_safe_float(inv.get("coverage_pct"), 0.0),
        runs_merged_count=_safe_int(inv.get("runs_merged_count"), 0),
        explore_sessions_count=_safe_int(inv.get("explore_sessions_count"), 0),
        by_prefix=by_prefix_items,
    )

    cap_raw = capability_map if isinstance(capability_map, dict) else {}
    cap_metrics = dict(cap_raw.get("metrics") or {}) if isinstance(cap_raw.get("metrics"), dict) else {}
    raw_caps = cap_raw.get("capabilities")
    cap_items: list[AcceptanceCapabilityItem] = []
    for cap in (raw_caps if isinstance(raw_caps, list) else []):
        if not isinstance(cap, dict):
            continue
        cap_items.append(
            AcceptanceCapabilityItem(
                key=str(cap.get("key") or ""),
                module=str(cap.get("module") or ""),
                resource=str(cap.get("resource") or ""),
                display_name=str(cap.get("display_name") or cap.get("key") or ""),
                title_zh=str(cap.get("title_zh") or ""),
                title_en=str(cap.get("title_en") or ""),
                recommended_priority=str(cap.get("recommended_priority") or "P2"),
                is_core=bool(cap.get("is_core", False)),
                total_endpoints=_safe_int(cap.get("total_endpoints"), 0),
                live_endpoints=_safe_int(cap.get("live_endpoints"), 0),
                live_pct=_safe_float(cap.get("live_pct"), 0.0),
                roles=[str(r) for r in (cap.get("roles") or []) if str(r).strip()],
                case_coverage_pct=_safe_float(cap.get("case_coverage_pct"), 0.0),
                case_matrix=_normalize_defense_slots(cap.get("case_matrix")),
            )
        )
    cap_map_model = AcceptanceCapabilityMap(metrics=cap_metrics, capabilities=cap_items)

    sc_list_dict = _to_dict(scenario_list) if scenario_list is not None else {}
    raw_sc_items = sc_list_dict.get("scenarios") if sc_list_dict else (scenarios or [])
    sc_items: list[AcceptanceScenarioItem] = []
    by_priority = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    by_effectiveness = {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0}
    p0_p1_total = 0
    p0_p1_effective_count = 0
    total_assertions = 0
    assertions_passed = 0
    assertions_failed = 0
    assertions_pending = 0
    total_negative_cases = 0
    negative_cases_covered = 0

    for raw_sc in raw_sc_items or []:
        sd = _to_dict(raw_sc)
        prio = str(sd.get("priority") or "P2").upper()
        if prio not in by_priority:
            prio = "P2"
        eff = str(sd.get("effectiveness_level") or "L0").upper()
        if eff not in by_effectiveness:
            eff = "L0"

        parsed_assertions: list[AcceptanceScenarioAssertionItem] = []
        for raw_a in sd.get("assertions") or []:
            ad = _to_dict(raw_a)
            st = str(ad.get("status") or "pending").lower()
            parsed_assertions.append(
                AcceptanceScenarioAssertionItem(
                    id=str(ad.get("id") or ""),
                    title=str(ad.get("title") or ""),
                    target_endpoint=str(ad.get("target_endpoint") or ""),
                    operator=str(ad.get("operator") or "not_empty"),
                    expected=str(ad.get("expected") or ""),
                    status=st,
                )
            )

        a_count = _safe_int(sd.get("assertion_count"), len(parsed_assertions))
        a_passed = _safe_int(
            sd.get("assertions_passed_count"),
            sum(1 for a in parsed_assertions if a.status == "passed"),
        )
        a_failed = sum(1 for a in parsed_assertions if a.status == "failed")
        a_pending = max(0, a_count - a_passed - a_failed)
        nc_count = _safe_int(sd.get("negative_case_count"), 0)
        nc_cov = _safe_int(sd.get("negative_cases_covered_count"), 0)

        by_priority[prio] += 1
        by_effectiveness[eff] += 1
        if prio in {"P0", "P1"}:
            p0_p1_total += 1
            if eff in {"L3", "L4"}:
                p0_p1_effective_count += 1

        total_assertions += a_count
        assertions_passed += a_passed
        assertions_failed += a_failed
        assertions_pending += a_pending
        total_negative_cases += nc_count
        negative_cases_covered += nc_cov

        sc_items.append(
            AcceptanceScenarioItem(
                id=str(sd.get("id") or ""),
                slug=str(sd.get("slug") or ""),
                name=str(sd.get("name") or sd.get("slug") or ""),
                priority=prio,
                status=str(sd.get("status") or "active"),
                effectiveness_level=eff,
                endpoint_count=_safe_int(sd.get("endpoint_count"), 0),
                run_count=_safe_int(sd.get("run_count"), 0),
                assertion_count=a_count,
                assertions_passed_count=a_passed,
                negative_case_count=nc_count,
                negative_cases_covered_count=nc_cov,
                assertions=parsed_assertions,
            )
        )

    if sc_list_dict:
        if isinstance(sc_list_dict.get("by_priority"), dict):
            for k in by_priority:
                by_priority[k] = _safe_int(sc_list_dict["by_priority"].get(k), by_priority[k])
        if isinstance(sc_list_dict.get("by_effectiveness"), dict):
            for k in by_effectiveness:
                by_effectiveness[k] = _safe_int(
                    sc_list_dict["by_effectiveness"].get(k), by_effectiveness[k]
                )
        if sc_list_dict.get("p0_p1_total") is not None:
            p0_p1_total = _safe_int(sc_list_dict["p0_p1_total"], p0_p1_total)
        if sc_list_dict.get("p0_p1_effective_count") is not None:
            p0_p1_effective_count = _safe_int(
                sc_list_dict["p0_p1_effective_count"], p0_p1_effective_count
            )

    p0_p1_effective_pct = (
        round((p0_p1_effective_count / p0_p1_total) * 100.0, 2) if p0_p1_total else 0.0
    )
    scenario_stats = AcceptanceScenarioStats(
        total=len(sc_items),
        by_priority=by_priority,
        by_effectiveness=by_effectiveness,
        p0_p1_total=p0_p1_total,
        p0_p1_effective_count=p0_p1_effective_count,
        p0_p1_effective_pct=p0_p1_effective_pct,
        total_assertions=total_assertions,
        assertions_passed=assertions_passed,
        assertions_failed=assertions_failed,
        assertions_pending=assertions_pending,
        total_negative_cases=total_negative_cases,
        negative_cases_covered=negative_cases_covered,
        scenarios=sc_items,
    )

    chain_items: list[AcceptanceChainEvidenceItem] = []
    passed_chains = 0
    failed_chains = 0
    eval_total = 0
    eval_passed = 0
    eval_failed = 0
    for raw_ch in chain_runs or []:
        ch = _to_dict(raw_ch)
        ch_status = str(ch.get("status") or "failed").lower()
        if ch_status == "passed":
            passed_chains += 1
        else:
            failed_chains += 1
        step_evs: list[AcceptanceChainStepEvidence] = []
        for raw_st in ch.get("step_results") or []:
            st = _to_dict(raw_st)
            a_evs: list[AcceptanceChainAssertionEvidence] = []
            for raw_ae in st.get("assertions") or []:
                ae = _to_dict(raw_ae)
                is_passed = bool(ae.get("passed", False))
                eval_total += 1
                if is_passed:
                    eval_passed += 1
                else:
                    eval_failed += 1
                a_evs.append(
                    AcceptanceChainAssertionEvidence(
                        id=str(ae.get("id") or ""),
                        title=str(ae.get("title") or ""),
                        operator=str(ae.get("operator") or ""),
                        passed=is_passed,
                        actual=ae.get("actual"),
                        expected=str(ae.get("expected") or ""),
                        message=str(ae.get("message") or ""),
                    )
                )
            step_evs.append(
                AcceptanceChainStepEvidence(
                    step_index=_safe_int(st.get("step_index"), 0),
                    step=_safe_int(st.get("step"), 1),
                    name=str(st.get("name") or ""),
                    method=str(st.get("method") or "GET").upper(),
                    url=str(st.get("url") or ""),
                    status=str(st.get("status") or "skipped").lower(),
                    status_code=_safe_int(st.get("status_code"), 0),
                    duration_ms=_safe_float(st.get("duration_ms"), 0.0),
                    error=str(st["error"]) if st.get("error") is not None else None,
                    assertions=a_evs,
                )
            )
        chain_items.append(
            AcceptanceChainEvidenceItem(
                chain_run_id=str(ch.get("chain_run_id") or ""),
                chain_name=str(ch.get("chain_name") or ""),
                run_id=str(ch["run_id"]) if ch.get("run_id") is not None else None,
                status=ch_status,
                total_steps=_safe_int(ch.get("total_steps"), len(step_evs)),
                completed_steps=_safe_int(ch.get("completed_steps"), 0),
                created_at=str(ch.get("created_at") or ""),
                updated_at=str(ch.get("updated_at") or ""),
                step_results=step_evs,
            )
        )

    total_chains = len(chain_items)
    chain_pass_rate = round((passed_chains / total_chains) * 100.0, 2) if total_chains else 0.0
    chain_summary = AcceptanceChainEvidenceSummary(
        total_chain_runs=total_chains,
        passed_chains=passed_chains,
        failed_chains=failed_chains,
        chain_pass_rate_pct=chain_pass_rate,
        total_assertions_evaluated=eval_total,
        assertions_passed=eval_passed,
        assertions_failed=eval_failed,
        chains=chain_items,
    )

    sec_by_sev: dict[str, int] = {}
    perf_by_sev: dict[str, int] = {}
    sec_total = 0
    perf_total = 0
    sec_hc: list[AcceptanceFindingItem] = []
    perf_hc: list[AcceptanceFindingItem] = []

    for raw_f in findings or []:
        f_item = _normalize_finding(raw_f)
        if _is_perf_category(f_item.category):
            perf_total += 1
            perf_by_sev[f_item.severity] = perf_by_sev.get(f_item.severity, 0) + 1
            if f_item.severity in {"high", "critical"}:
                perf_hc.append(f_item)
        else:
            sec_total += 1
            sec_by_sev[f_item.severity] = sec_by_sev.get(f_item.severity, 0) + 1
            if f_item.severity in {"high", "critical"}:
                sec_hc.append(f_item)

    sec_hc.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.category, f.endpoint, f.title))
    perf_hc.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.category, f.endpoint, f.title))

    har_summary = None
    slowest_ttfb: list[dict[str, Any]] = []
    if isinstance(har_perf_report, dict):
        if isinstance(har_perf_report.get("summary"), dict):
            har_summary = dict(har_perf_report["summary"])
        raw_slow = har_perf_report.get("slowest_ttfb")
        if isinstance(raw_slow, list):
            slowest_ttfb = [dict(x) for x in raw_slow[:10] if isinstance(x, dict)]

    k6_clean = dict(k6_analysis) if isinstance(k6_analysis, dict) else None

    sec_model = AcceptanceSecurityFindings(
        total_count=sec_total,
        by_severity=sec_by_sev,
        high_critical_count=len(sec_hc),
        high_critical_items=sec_hc,
    )
    perf_model = AcceptancePerformanceSummary(
        total_count=perf_total,
        by_severity=perf_by_sev,
        high_critical_count=len(perf_hc),
        high_critical_items=perf_hc,
        har_perf_summary=har_summary,
        slowest_ttfb=slowest_ttfb,
        k6_analysis=k6_clean,
    )

    cap_cov_pct = _safe_float(cap_metrics.get("capability_coverage_pct"), 0.0)
    if sec_model.high_critical_count > 0 or chain_summary.failed_chains > 0:
        overall: Literal["passed", "attention_needed", "failed"] = "failed"
    elif (
        perf_model.high_critical_count > 0
        or (scenario_stats.p0_p1_total > 0 and scenario_stats.p0_p1_effective_pct < 100.0)
        or (api_coverage.total_endpoints > 0 and api_coverage.coverage_pct < 50.0)
    ):
        overall = "attention_needed"
    else:
        overall = "passed"

    summary = AcceptanceSummaryVerdict(
        overall_status=overall,
        api_coverage_pct=api_coverage.coverage_pct,
        capability_coverage_pct=cap_cov_pct,
        p0_p1_effective_pct=scenario_stats.p0_p1_effective_pct,
        chain_pass_rate_pct=chain_summary.chain_pass_rate_pct,
        high_critical_security_count=sec_model.high_critical_count,
        high_critical_perf_count=perf_model.high_critical_count,
    )

    return AcceptanceReportData(
        project_id=str(project_id),
        project_name=str(project_name or project_id),
        project_slug=str(project_slug or ""),
        run_id=run_id,
        generated_at=generated_at or datetime.now(UTC).isoformat(),
        summary=summary,
        api_coverage=api_coverage,
        capability_map=cap_map_model,
        scenarios=scenario_stats,
        chain_evidence=chain_summary,
        security_findings=sec_model,
        performance=perf_model,
    )


def _coerce_report(data: AcceptanceReportData | dict[str, Any]) -> AcceptanceReportData:
    if isinstance(data, AcceptanceReportData):
        return data
    if isinstance(data, dict):
        # Allow callers passing either full AcceptanceReportData dict or partial dict
        if "summary" in data and "api_coverage" in data and "generated_at" in data:
            return AcceptanceReportData.model_validate(data)
        return build_acceptance_report_data(
            project_id=str(data.get("project_id") or "unknown"),
            project_name=str(data.get("project_name") or data.get("project_id") or "Project"),
            project_slug=str(data.get("project_slug") or ""),
            run_id=data.get("run_id"),
            inventory=data.get("inventory") or data.get("api_coverage"),
            capability_map=data.get("capability_map"),
            scenario_list=data.get("scenarios") if isinstance(data.get("scenarios"), dict) else None,
            scenarios=data.get("scenarios") if isinstance(data.get("scenarios"), list) else None,
            chain_runs=data.get("chain_runs")
            or (
                data["chain_evidence"].get("chains")
                if isinstance(data.get("chain_evidence"), dict)
                else None
            ),
            findings=data.get("findings"),
            har_perf_report=data.get("har_perf_report"),
            k6_analysis=data.get("k6_analysis"),
            generated_at=data.get("generated_at"),
        )
    raise TypeError(f"Unsupported acceptance report payload type: {type(data)!r}")


def _md_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("\r\n", " ").replace("\n", " ").replace("|", "\\|")


def _format_actual_preview(value: Any, max_len: int = 120) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def render_acceptance_report_markdown(data: AcceptanceReportData | dict[str, Any]) -> str:
    """Render AcceptanceReportData into standalone GitHub-Flavored Markdown."""
    report = _coerce_report(data)
    s = report.summary
    cov = report.api_coverage
    cap = report.capability_map
    sc = report.scenarios
    ce = report.chain_evidence
    sec = report.security_findings
    perf = report.performance

    lines: list[str] = [
        f"# Acceptance Report: {_md_cell(report.project_name)}",
        "",
        f"- **Project ID**: `{_md_cell(report.project_id)}`",
        f"- **Run ID**: `{_md_cell(report.run_id or 'all')}`",
        f"- **Generated At**: `{_md_cell(report.generated_at)}`",
        f"- **Overall Status**: `{s.overall_status.upper()}`",
        "",
        "## Executive Summary",
        "",
        "| KPI | Value |",
        "| --- | ---: |",
        f"| API Coverage | {s.api_coverage_pct:.2f}% ({cov.har_live_count}/{cov.total_endpoints}) |",
        f"| Capability Coverage | {s.capability_coverage_pct:.2f}% |",
        f"| P0/P1 Effective (L3/L4) | {s.p0_p1_effective_pct:.2f}% ({sc.p0_p1_effective_count}/{sc.p0_p1_total}) |",
        f"| Chain Pass Rate | {s.chain_pass_rate_pct:.2f}% ({ce.passed_chains}/{ce.total_chain_runs}) |",
        f"| High/Critical Security Findings | {s.high_critical_security_count} |",
        f"| High/Critical Performance Findings | {s.high_critical_perf_count} |",
        "",
        "## 1. API Coverage",
        "",
        f"- Total Endpoints: **{cov.total_endpoints}**",
        f"- HAR Live Covered: **{cov.har_live_count}**",
        f"- JS Static Discovered: **{cov.js_static_count}**",
        f"- Coverage: **{cov.coverage_pct:.2f}%**",
        f"- Completed Runs Merged: **{cov.runs_merged_count}** | Explore Sessions: **{cov.explore_sessions_count}**",
        "",
    ]

    if cov.by_prefix:
        lines.extend(
            [
                "| Prefix | Covered | Total | Coverage % |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for p in cov.by_prefix:
            lines.append(
                f"| `{_md_cell(p.prefix)}` | {p.covered} | {p.total} | {p.coverage_pct:.2f}% |"
            )
    else:
        lines.append("_No prefix coverage breakdown recorded._")

    lines.extend(
        [
            "",
            "## 2. Capability Map & 5-Defense Matrix",
            "",
        ]
    )
    m = cap.metrics
    if m:
        live_caps = _safe_int(m.get("live_capabilities"), 0)
        total_caps = _safe_int(m.get("total_capabilities"), len(cap.capabilities))
        core_caps = _safe_int(m.get("core_capabilities"), 0)
        cap_cov_pct = _safe_float(m.get("capability_coverage_pct"), 0.0)
        slots_passed = _safe_int(m.get("slots_passed"), 0)
        slots_failed = _safe_int(m.get("slots_failed"), 0)
        slots_pending = _safe_int(m.get("slots_pending"), 0)
        slots_missing = _safe_int(m.get("slots_missing"), 0)
        state_cov_pct = _safe_float(m.get("state_coverage_pct"), 0.0)
        role_cov_pct = _safe_float(m.get("role_matrix_coverage_pct"), 0.0)
        neg_cov_pct = _safe_float(m.get("negative_coverage_pct"), 0.0)
        lines.extend(
            [
                (
                    f"- Capabilities: **{live_caps}/{total_caps}** live "
                    f"(Core: **{core_caps}**, Capability Coverage: **{cap_cov_pct:.2f}%**)"
                ),
                (
                    f"- Defense Slots — Passed/Covered: **{slots_passed}**, "
                    f"Failed: **{slots_failed}**, "
                    f"Pending: **{slots_pending}**, "
                    f"Missing: **{slots_missing}**"
                ),
                (
                    f"- State Coverage: **{state_cov_pct:.2f}%** | "
                    f"Role Matrix: **{role_cov_pct:.2f}%** | "
                    f"Negative Coverage: **{neg_cov_pct:.2f}%**"
                ),
                "",
            ]
        )

    if cap.capabilities:
        lines.extend(
            [
                "| Capability | Priority | Live/Total | Roles | Case % | Happy Path | Invalid Params | Illegal State | AuthZ / Role | Consistency / Idempotency |",
                "| --- | :---: | ---: | --- | ---: | :---: | :---: | :---: | :---: | :---: |",
            ]
        )
        for c in cap.capabilities:
            slot_map = {sl.slot: sl for sl in c.case_matrix}
            slot_cells = [
                f"{slot_map[k].status} ({slot_map[k].count})" if k in slot_map else "missing (0)"
                for k in _DEFENSE_SLOTS
            ]
            roles_str = ", ".join(c.roles) if c.roles else "-"
            lines.append(
                f"| {_md_cell(c.display_name or c.key)} | {_md_cell(c.recommended_priority)} | "
                f"{c.live_endpoints}/{c.total_endpoints} | {_md_cell(roles_str)} | "
                f"{c.case_coverage_pct:.1f}% | " + " | ".join(slot_cells) + " |"
            )
    else:
        lines.append("_No capabilities discovered._")

    lines.extend(
        [
            "",
            "## 3. P0/P1 L0->L4 Scenario Statistics",
            "",
            f"- Total Scenarios: **{sc.total}**",
            f"- By Priority: P0={sc.by_priority.get('P0', 0)}, P1={sc.by_priority.get('P1', 0)}, P2={sc.by_priority.get('P2', 0)}, P3={sc.by_priority.get('P3', 0)}",
            (
                f"- By Effectiveness: L0={sc.by_effectiveness.get('L0', 0)}, "
                f"L1={sc.by_effectiveness.get('L1', 0)}, L2={sc.by_effectiveness.get('L2', 0)}, "
                f"L3={sc.by_effectiveness.get('L3', 0)}, L4={sc.by_effectiveness.get('L4', 0)}"
            ),
            f"- P0/P1 Effective (L3/L4): **{sc.p0_p1_effective_count}/{sc.p0_p1_total}** (**{sc.p0_p1_effective_pct:.2f}%**)",
            (
                f"- Scenario Assertions: **{sc.total_assertions}** total "
                f"(passed={sc.assertions_passed}, failed={sc.assertions_failed}, pending={sc.assertions_pending}) | "
                f"Negative Cases Covered: **{sc.negative_cases_covered}/{sc.total_negative_cases}**"
            ),
            "",
        ]
    )
    if sc.scenarios:
        lines.extend(
            [
                "| Scenario | Priority | Level | Status | Endpoints | Assertions (Pass/Total) | Negative (Cov/Total) |",
                "| --- | :---: | :---: | :---: | ---: | ---: | ---: |",
            ]
        )
        for item in sc.scenarios:
            lines.append(
                f"| {_md_cell(item.name)} (`{_md_cell(item.slug)}`) | {_md_cell(item.priority)} | "
                f"{_md_cell(item.effectiveness_level)} | {_md_cell(item.status)} | "
                f"{item.endpoint_count} | {item.assertions_passed_count}/{item.assertion_count} | "
                f"{item.negative_cases_covered_count}/{item.negative_case_count} |"
            )
    else:
        lines.append("_No scenarios recorded._")

    lines.extend(
        [
            "",
            "## 4. Chain Assertion Execution Evidence",
            "",
            (
                f"- Chain Runs: **{ce.total_chain_runs}** total "
                f"(passed={ce.passed_chains}, failed={ce.failed_chains}, pass_rate={ce.chain_pass_rate_pct:.2f}%)"
            ),
            (
                f"- Evaluated Assertions: **{ce.total_assertions_evaluated}** "
                f"(passed={ce.assertions_passed}, failed={ce.assertions_failed})"
            ),
            "",
        ]
    )
    if ce.chains:
        for ch in ce.chains:
            lines.append(
                f"### Chain: {_md_cell(ch.chain_name)} (`{_md_cell(ch.chain_run_id)}`) — **{ch.status.upper()}** ({ch.completed_steps}/{ch.total_steps} steps)"
            )
            lines.append("")
            if ch.step_results:
                lines.extend(
                    [
                        "| Step | Request | Status | HTTP | Duration (ms) | Assertions |",
                        "| ---: | --- | :---: | ---: | ---: | --- |",
                    ]
                )
                for st in ch.step_results:
                    a_summary = ", ".join(
                        f"{'PASS' if a.passed else 'FAIL'}:{_md_cell(a.title or a.operator)}"
                        for a in st.assertions
                    ) or "-"
                    lines.append(
                        f"| {st.step} | `{_md_cell(st.method)} {_md_cell(st.url)}` | "
                        f"{_md_cell(st.status)} | {st.status_code} | {st.duration_ms:.1f} | {a_summary} |"
                    )
                for st in ch.step_results:
                    if st.assertions:
                        lines.append("")
                        lines.append(f"Step {st.step} Assertion Details:")
                        lines.append("")
                        lines.append("| Title | Operator | Passed | Expected | Actual | Message |")
                        lines.append("| --- | --- | :---: | --- | --- | --- |")
                        for a in st.assertions:
                            lines.append(
                                f"| {_md_cell(a.title)} | `{_md_cell(a.operator)}` | "
                                f"{'PASS' if a.passed else 'FAIL'} | `{_md_cell(a.expected)}` | "
                                f"`{_md_cell(_format_actual_preview(a.actual))}` | {_md_cell(a.message)} |"
                            )
            lines.append("")
    else:
        lines.append("_No chain execution checkpoints recorded._")

    scenario_assertions_rows = [
        (item, a) for item in sc.scenarios for a in item.assertions
    ]
    if scenario_assertions_rows:
        lines.extend(
            [
                "",
                "### Scenario Assertion Statuses",
                "",
                "| Scenario | Assertion | Target Endpoint | Operator | Expected | Status |",
                "| --- | --- | --- | --- | --- | :---: |",
            ]
        )
        for item, a in scenario_assertions_rows:
            lines.append(
                f"| {_md_cell(item.name)} | {_md_cell(a.title)} | `{_md_cell(a.target_endpoint)}` | "
                f"`{_md_cell(a.operator)}` | `{_md_cell(a.expected)}` | {_md_cell(a.status)} |"
            )

    lines.extend(
        [
            "",
            "## 5. High & Critical Security and Performance Findings",
            "",
            "### Security Findings (High / Critical)",
            "",
            f"- Total Security Findings: **{sec.total_count}** | High/Critical: **{sec.high_critical_count}**",
            "",
        ]
    )
    if sec.high_critical_items:
        lines.extend(
            [
                "| Severity | Category | Endpoint | Title |",
                "| :---: | --- | --- | --- |",
            ]
        )
        for f in sec.high_critical_items:
            lines.append(
                f"| {f.severity.upper()} | `{_md_cell(f.category)}` | `{_md_cell(f.endpoint)}` | {_md_cell(f.title)} |"
            )
    else:
        lines.append("_No High or Critical security findings recorded._")

    lines.extend(
        [
            "",
            "### Performance Findings & Summary",
            "",
            f"- Total Performance Findings: **{perf.total_count}** | High/Critical: **{perf.high_critical_count}**",
            "",
        ]
    )
    if perf.high_critical_items:
        lines.extend(
            [
                "| Severity | Category | Endpoint | Title |",
                "| :---: | --- | --- | --- |",
            ]
        )
        for f in perf.high_critical_items:
            lines.append(
                f"| {f.severity.upper()} | `{_md_cell(f.category)}` | `{_md_cell(f.endpoint)}` | {_md_cell(f.title)} |"
            )
        lines.append("")

    if perf.har_perf_summary:
        hp = perf.har_perf_summary
        lines.extend(
            [
                (
                    f"- HAR Performance Summary: requests=**{hp.get('total_requests', 0)}**, "
                    f"avg_ttfb=**{hp.get('avg_ttfb_ms', 0)}ms**, "
                    f"p95_ttfb=**{hp.get('p95_ttfb_ms', 0)}ms**, "
                    f"p95_total=**{hp.get('p95_total_ms', 0)}ms**, "
                    f"large_bodies=**{hp.get('large_body_count', 0)}**"
                ),
                "",
            ]
        )

    if perf.k6_analysis:
        k6 = perf.k6_analysis
        k6_m = k6.get("metrics") if isinstance(k6.get("metrics"), dict) else {}
        lat = k6_m.get("latency_ms") if isinstance(k6_m.get("latency_ms"), dict) else {}
        diag = k6.get("diagnosis") if isinstance(k6.get("diagnosis"), dict) else {}
        lines.append(
            f"- k6 Load Analysis: status=`{_md_cell(k6.get('status', 'unknown'))}`, "
            f"rps=**{k6_m.get('rps', 0)}**, p95=**{lat.get('p95', 0)}ms**, "
            f"failed_rate=**{k6_m.get('http_req_failed_rate', 0)}**"
        )
        if k6.get("reason"):
            lines.append(f"  - Reason: {_md_cell(k6['reason'])}")
        breached = _normalize_str_list(diag.get("breached_thresholds"))
        if breached:
            lines.append(
                "  - Breached Thresholds: "
                + ", ".join(f"`{_md_cell(t)}`" for t in breached)
            )
        for note in _normalize_str_list(diag.get("notes")):
            lines.append(f"  - Note: {_md_cell(note)}")

    return "\n".join(lines).rstrip() + "\n"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _badge(label: Any, variant: str = "neutral") -> str:
    safe_variant = variant if variant in {"passed", "failed", "warn", "info", "neutral"} else "neutral"
    return f'<span class="badge badge-{safe_variant}">{_esc(label)}</span>'


def _slot_variant(status: str) -> str:
    low = status.lower()
    if low in {"covered", "passed"}:
        return "passed"
    if low == "failed":
        return "failed"
    if low == "pending":
        return "warn"
    return "neutral"


def _sev_variant(severity: str) -> str:
    low = severity.lower()
    if low == "critical":
        return "failed"
    if low == "high":
        return "warn"
    return "info"


_HTML_CSS = """
:root {
  --bg: #f8fafc;
  --surface: #ffffff;
  --border: #e2e8f0;
  --text: #0f172a;
  --muted: #475569;
  --accent: #1d4ed8;
  --pass-bg: #dcfce7;
  --pass-fg: #166534;
  --fail-bg: #fee2e2;
  --fail-fg: #991b1b;
  --warn-bg: #fef3c7;
  --warn-fg: #92400e;
  --info-bg: #dbeafe;
  --info-fg: #1e40af;
  --neut-bg: #f1f5f9;
  --neut-fg: #475569;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  font-size: 14px;
}
.container { max-width: 1240px; margin: 0 auto; }
.header {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 24px;
  margin-bottom: 20px;
}
.header h1 { margin: 0 0 8px 0; font-size: 22px; }
.meta { color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; gap: 16px; }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.kpi-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.kpi-value { font-size: 22px; font-weight: 700; margin-top: 4px; }
.kpi-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 24px;
  margin-bottom: 20px;
}
section h2 { margin: 0 0 12px 0; font-size: 17px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
section h3 { margin: 16px 0 8px 0; font-size: 15px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
th, td { border: 1px solid var(--border); padding: 7px 10px; text-align: left; vertical-align: top; }
th { background: #f1f5f9; font-weight: 600; }
tr:nth-child(even) td { background: #fafcff; }
code { background: #f1f5f9; padding: 1px 5px; border-radius: 4px; font-size: 12px; word-break: break-word; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}
.badge-passed { background: var(--pass-bg); color: var(--pass-fg); }
.badge-failed { background: var(--fail-bg); color: var(--fail-fg); }
.badge-warn { background: var(--warn-bg); color: var(--warn-fg); }
.badge-info { background: var(--info-bg); color: var(--info-fg); }
.badge-neutral { background: var(--neut-bg); color: var(--neut-fg); }
.empty { color: var(--muted); font-style: italic; margin: 8px 0; }
.chain-block { border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-top: 12px; }
@media print {
  body { background: #ffffff; padding: 0; }
  section, .header, .kpi-card { break-inside: avoid; }
}
""".strip()


def render_acceptance_report_html(data: AcceptanceReportData | dict[str, Any]) -> str:
    """Render AcceptanceReportData into a self-contained, XSS-safe single-file HTML5 report."""
    report = _coerce_report(data)
    s = report.summary
    cov = report.api_coverage
    cap = report.capability_map
    sc = report.scenarios
    ce = report.chain_evidence
    sec = report.security_findings
    perf = report.performance

    overall_variant = (
        "passed"
        if s.overall_status == "passed"
        else ("failed" if s.overall_status == "failed" else "warn")
    )

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Acceptance Report — {_esc(report.project_name)}</title>",
        f"<style>{_HTML_CSS}</style>",
        "</head>",
        "<body>",
        '<div class="container">',
        '<div class="header">',
        f"<h1>Acceptance Report: {_esc(report.project_name)} {_badge(s.overall_status.upper(), overall_variant)}</h1>",
        '<div class="meta">',
        f"<span>Project ID: <code>{_esc(report.project_id)}</code></span>",
        f"<span>Run ID: <code>{_esc(report.run_id or 'all')}</code></span>",
        f"<span>Generated At: <code>{_esc(report.generated_at)}</code></span>",
        "</div>",
        "</div>",
        '<div class="kpi-grid">',
        (
            '<div class="kpi-card"><div class="kpi-label">API Coverage</div>'
            f'<div class="kpi-value">{s.api_coverage_pct:.2f}%</div>'
            f'<div class="kpi-sub">{cov.har_live_count} / {cov.total_endpoints} endpoints</div></div>'
        ),
        (
            '<div class="kpi-card"><div class="kpi-label">Capability Coverage</div>'
            f'<div class="kpi-value">{s.capability_coverage_pct:.2f}%</div>'
            f'<div class="kpi-sub">{len(cap.capabilities)} capabilities</div></div>'
        ),
        (
            '<div class="kpi-card"><div class="kpi-label">P0/P1 Effective (L3/L4)</div>'
            f'<div class="kpi-value">{s.p0_p1_effective_pct:.2f}%</div>'
            f'<div class="kpi-sub">{sc.p0_p1_effective_count} / {sc.p0_p1_total} P0/P1</div></div>'
        ),
        (
            '<div class="kpi-card"><div class="kpi-label">Chain Pass Rate</div>'
            f'<div class="kpi-value">{s.chain_pass_rate_pct:.2f}%</div>'
            f'<div class="kpi-sub">{ce.passed_chains} / {ce.total_chain_runs} chains</div></div>'
        ),
        (
            '<div class="kpi-card"><div class="kpi-label">High+ Security</div>'
            f'<div class="kpi-value">{s.high_critical_security_count}</div>'
            f'<div class="kpi-sub">{sec.total_count} total findings</div></div>'
        ),
        (
            '<div class="kpi-card"><div class="kpi-label">High+ Performance</div>'
            f'<div class="kpi-value">{s.high_critical_perf_count}</div>'
            f'<div class="kpi-sub">{perf.total_count} total perf findings</div></div>'
        ),
        "</div>",
    ]

    # Section 1: API Coverage
    parts.append("<section><h2>1. API Coverage</h2>")
    parts.append(
        "<p>"
        f"Total Endpoints: <strong>{cov.total_endpoints}</strong> | "
        f"HAR Live: <strong>{cov.har_live_count}</strong> | "
        f"JS Static: <strong>{cov.js_static_count}</strong> | "
        f"Coverage: <strong>{cov.coverage_pct:.2f}%</strong> | "
        f"Runs Merged: <strong>{cov.runs_merged_count}</strong> | "
        f"Explore Sessions: <strong>{cov.explore_sessions_count}</strong>"
        "</p>"
    )
    if cov.by_prefix:
        parts.append(
            "<table><thead><tr><th>Prefix</th><th>Covered</th><th>Total</th><th>Coverage %</th></tr></thead><tbody>"
        )
        for p in cov.by_prefix:
            parts.append(
                f"<tr><td><code>{_esc(p.prefix)}</code></td><td>{p.covered}</td>"
                f"<td>{p.total}</td><td>{p.coverage_pct:.2f}%</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append('<p class="empty">No prefix coverage breakdown recorded.</p>')
    parts.append("</section>")

    # Section 2: Capability Map 5-Defense Matrix
    parts.append("<section><h2>2. Capability Map &amp; 5-Defense Matrix</h2>")
    m = cap.metrics
    if m:
        live_caps = _safe_int(m.get("live_capabilities"), 0)
        total_caps = _safe_int(m.get("total_capabilities"), len(cap.capabilities))
        state_cov_pct = _safe_float(m.get("state_coverage_pct"), 0.0)
        role_cov_pct = _safe_float(m.get("role_matrix_coverage_pct"), 0.0)
        neg_cov_pct = _safe_float(m.get("negative_coverage_pct"), 0.0)
        slots_passed = _safe_int(m.get("slots_passed"), 0)
        slots_failed = _safe_int(m.get("slots_failed"), 0)
        slots_pending = _safe_int(m.get("slots_pending"), 0)
        slots_missing = _safe_int(m.get("slots_missing"), 0)
        parts.append(
            "<p>"
            f"Live Capabilities: <strong>{live_caps}/{total_caps}</strong> | "
            f"State Coverage: <strong>{state_cov_pct:.2f}%</strong> | "
            f"Role Matrix: <strong>{role_cov_pct:.2f}%</strong> | "
            f"Negative Coverage: <strong>{neg_cov_pct:.2f}%</strong> | "
            f"Slots Passed/Failed/Pending/Missing: "
            f"<strong>{slots_passed}/{slots_failed}/{slots_pending}/{slots_missing}</strong>"
            "</p>"
        )
    if cap.capabilities:
        slot_headers = "".join(f"<th>{_esc(_SLOT_LABELS[k])}</th>" for k in _DEFENSE_SLOTS)
        parts.append(
            "<table><thead><tr><th>Capability</th><th>Priority</th><th>Live/Total</th>"
            f"<th>Roles</th><th>Case %</th>{slot_headers}</tr></thead><tbody>"
        )
        for c in cap.capabilities:
            slot_map = {sl.slot: sl for sl in c.case_matrix}
            slot_tds = []
            for k in _DEFENSE_SLOTS:
                sl = slot_map.get(k, AcceptanceDefenseSlot(slot=k))
                slot_tds.append(f"<td>{_badge(f'{sl.status} ({sl.count})', _slot_variant(sl.status))}</td>")
            roles_str = ", ".join(c.roles) if c.roles else "-"
            parts.append(
                f"<tr><td>{_esc(c.display_name or c.key)}</td>"
                f"<td>{_badge(c.recommended_priority, 'info')}</td>"
                f"<td>{c.live_endpoints}/{c.total_endpoints}</td>"
                f"<td>{_esc(roles_str)}</td>"
                f"<td>{c.case_coverage_pct:.1f}%</td>"
                + "".join(slot_tds)
                + "</tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append('<p class="empty">No capabilities discovered.</p>')
    parts.append("</section>")

    # Section 3: P0/P1 L0->L4 Scenario Statistics
    parts.append("<section><h2>3. P0/P1 L0-&gt;L4 Scenario Statistics</h2>")
    parts.append(
        "<p>"
        f"Total Scenarios: <strong>{sc.total}</strong> | "
        f"P0/P1 Effective (L3/L4): <strong>{sc.p0_p1_effective_count}/{sc.p0_p1_total} ({sc.p0_p1_effective_pct:.2f}%)</strong> | "
        f"By Priority: P0={sc.by_priority.get('P0', 0)}, P1={sc.by_priority.get('P1', 0)}, "
        f"P2={sc.by_priority.get('P2', 0)}, P3={sc.by_priority.get('P3', 0)} | "
        f"By Effectiveness: L0={sc.by_effectiveness.get('L0', 0)}, L1={sc.by_effectiveness.get('L1', 0)}, "
        f"L2={sc.by_effectiveness.get('L2', 0)}, L3={sc.by_effectiveness.get('L3', 0)}, L4={sc.by_effectiveness.get('L4', 0)}"
        "</p>"
    )
    parts.append(
        "<p>"
        f"Assertions: <strong>{sc.total_assertions}</strong> total "
        f"(passed={sc.assertions_passed}, failed={sc.assertions_failed}, pending={sc.assertions_pending}) | "
        f"Negative Cases Covered: <strong>{sc.negative_cases_covered}/{sc.total_negative_cases}</strong>"
        "</p>"
    )
    if sc.scenarios:
        parts.append(
            "<table><thead><tr><th>Scenario</th><th>Priority</th><th>Level</th>"
            "<th>Status</th><th>Endpoints</th><th>Assertions (Pass/Total)</th>"
            "<th>Negative (Cov/Total)</th></tr></thead><tbody>"
        )
        for item in sc.scenarios:
            lvl_var = "passed" if item.effectiveness_level in {"L3", "L4"} else "warn"
            parts.append(
                f"<tr><td>{_esc(item.name)} <code>{_esc(item.slug)}</code></td>"
                f"<td>{_badge(item.priority, 'info')}</td>"
                f"<td>{_badge(item.effectiveness_level, lvl_var)}</td>"
                f"<td>{_esc(item.status)}</td>"
                f"<td>{item.endpoint_count}</td>"
                f"<td>{item.assertions_passed_count}/{item.assertion_count}</td>"
                f"<td>{item.negative_cases_covered_count}/{item.negative_case_count}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append('<p class="empty">No scenarios recorded.</p>')
    parts.append("</section>")

    # Section 4: Chain Assertion Execution Evidence
    parts.append("<section><h2>4. Chain Assertion Execution Evidence</h2>")
    parts.append(
        "<p>"
        f"Chain Runs: <strong>{ce.total_chain_runs}</strong> "
        f"(passed={ce.passed_chains}, failed={ce.failed_chains}, pass_rate={ce.chain_pass_rate_pct:.2f}%) | "
        f"Assertions Evaluated: <strong>{ce.total_assertions_evaluated}</strong> "
        f"(passed={ce.assertions_passed}, failed={ce.assertions_failed})"
        "</p>"
    )
    if ce.chains:
        for ch in ce.chains:
            ch_var = "passed" if ch.status == "passed" else "failed"
            parts.append('<div class="chain-block">')
            parts.append(
                f"<h3>{_esc(ch.chain_name)} <code>{_esc(ch.chain_run_id)}</code> "
                f"{_badge(ch.status.upper(), ch_var)} ({ch.completed_steps}/{ch.total_steps} steps)</h3>"
            )
            if ch.step_results:
                parts.append(
                    "<table><thead><tr><th>Step</th><th>Request</th><th>Status</th>"
                    "<th>HTTP</th><th>Duration (ms)</th><th>Error</th></tr></thead><tbody>"
                )
                for st in ch.step_results:
                    st_var = (
                        "passed"
                        if st.status == "passed"
                        else ("failed" if st.status == "failed" else "neutral")
                    )
                    parts.append(
                        f"<tr><td>{st.step}</td>"
                        f"<td><code>{_esc(st.method)} {_esc(st.url)}</code></td>"
                        f"<td>{_badge(st.status, st_var)}</td>"
                        f"<td>{st.status_code}</td>"
                        f"<td>{st.duration_ms:.1f}</td>"
                        f"<td>{_esc(st.error or '-')}</td></tr>"
                    )
                parts.append("</tbody></table>")
                all_step_evals = [(st.step, a) for st in ch.step_results for a in st.assertions]
                if all_step_evals:
                    parts.append(
                        "<table><thead><tr><th>Step</th><th>Assertion</th><th>Operator</th>"
                        "<th>Result</th><th>Expected</th><th>Actual</th><th>Message</th></tr></thead><tbody>"
                    )
                    for step_num, a in all_step_evals:
                        parts.append(
                            f"<tr><td>{step_num}</td>"
                            f"<td>{_esc(a.title)}</td>"
                            f"<td><code>{_esc(a.operator)}</code></td>"
                            f"<td>{_badge('PASS' if a.passed else 'FAIL', 'passed' if a.passed else 'failed')}</td>"
                            f"<td><code>{_esc(a.expected)}</code></td>"
                            f"<td><code>{_esc(_format_actual_preview(a.actual))}</code></td>"
                            f"<td>{_esc(a.message)}</td></tr>"
                        )
                    parts.append("</tbody></table>")
            parts.append("</div>")
    else:
        parts.append('<p class="empty">No chain execution checkpoints recorded.</p>')

    scenario_assertions_rows = [(item, a) for item in sc.scenarios for a in item.assertions]
    if scenario_assertions_rows:
        parts.append("<h3>Scenario Assertion Statuses</h3>")
        parts.append(
            "<table><thead><tr><th>Scenario</th><th>Assertion</th><th>Target Endpoint</th>"
            "<th>Operator</th><th>Expected</th><th>Status</th></tr></thead><tbody>"
        )
        for item, a in scenario_assertions_rows:
            parts.append(
                f"<tr><td>{_esc(item.name)}</td><td>{_esc(a.title)}</td>"
                f"<td><code>{_esc(a.target_endpoint)}</code></td>"
                f"<td><code>{_esc(a.operator)}</code></td>"
                f"<td><code>{_esc(a.expected)}</code></td>"
                f"<td>{_badge(a.status, _slot_variant(a.status))}</td></tr>"
            )
        parts.append("</tbody></table>")
    parts.append("</section>")

    # Section 5: High/Critical Security & Performance Findings
    parts.append("<section><h2>5. High &amp; Critical Security and Performance Findings</h2>")
    parts.append("<h3>Security Findings (High / Critical)</h3>")
    parts.append(
        f"<p>Total Security Findings: <strong>{sec.total_count}</strong> | "
        f"High/Critical: <strong>{sec.high_critical_count}</strong></p>"
    )
    if sec.high_critical_items:
        parts.append(
            "<table><thead><tr><th>Severity</th><th>Category</th><th>Endpoint</th>"
            "<th>Title</th><th>Evidence</th></tr></thead><tbody>"
        )
        for f in sec.high_critical_items:
            ev_str = _format_actual_preview(f.evidence, max_len=160) if f.evidence else "-"
            parts.append(
                f"<tr><td>{_badge(f.severity.upper(), _sev_variant(f.severity))}</td>"
                f"<td><code>{_esc(f.category)}</code></td>"
                f"<td><code>{_esc(f.endpoint)}</code></td>"
                f"<td>{_esc(f.title)}</td>"
                f"<td><code>{_esc(ev_str)}</code></td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append('<p class="empty">No High or Critical security findings recorded.</p>')

    parts.append("<h3>Performance Findings &amp; Load Summary</h3>")
    parts.append(
        f"<p>Total Performance Findings: <strong>{perf.total_count}</strong> | "
        f"High/Critical: <strong>{perf.high_critical_count}</strong></p>"
    )
    if perf.high_critical_items:
        parts.append(
            "<table><thead><tr><th>Severity</th><th>Category</th><th>Endpoint</th>"
            "<th>Title</th><th>Evidence</th></tr></thead><tbody>"
        )
        for f in perf.high_critical_items:
            ev_str = _format_actual_preview(f.evidence, max_len=160) if f.evidence else "-"
            parts.append(
                f"<tr><td>{_badge(f.severity.upper(), _sev_variant(f.severity))}</td>"
                f"<td><code>{_esc(f.category)}</code></td>"
                f"<td><code>{_esc(f.endpoint)}</code></td>"
                f"<td>{_esc(f.title)}</td>"
                f"<td><code>{_esc(ev_str)}</code></td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append('<p class="empty">No High or Critical performance findings recorded.</p>')

    if perf.har_perf_summary:
        hp = perf.har_perf_summary
        parts.append(
            "<p>"
            f"HAR Profiler: requests=<strong>{_esc(hp.get('total_requests', 0))}</strong> | "
            f"avg_ttfb=<strong>{_esc(hp.get('avg_ttfb_ms', 0))}ms</strong> | "
            f"p95_ttfb=<strong>{_esc(hp.get('p95_ttfb_ms', 0))}ms</strong> | "
            f"p95_total=<strong>{_esc(hp.get('p95_total_ms', 0))}ms</strong> | "
            f"large_bodies=<strong>{_esc(hp.get('large_body_count', 0))}</strong>"
            "</p>"
        )
    if perf.k6_analysis:
        k6 = perf.k6_analysis
        k6_m = k6.get("metrics") if isinstance(k6.get("metrics"), dict) else {}
        lat = k6_m.get("latency_ms") if isinstance(k6_m.get("latency_ms"), dict) else {}
        diag = k6.get("diagnosis") if isinstance(k6.get("diagnosis"), dict) else {}
        parts.append(
            "<p>"
            f"k6 Load Analysis: status=<code>{_esc(k6.get('status', 'unknown'))}</code> | "
            f"rps=<strong>{_esc(k6_m.get('rps', 0))}</strong> | "
            f"p95=<strong>{_esc(lat.get('p95', 0))}ms</strong> | "
            f"failed_rate=<strong>{_esc(k6_m.get('http_req_failed_rate', 0))}</strong>"
            "</p>"
        )
        if k6.get("reason"):
            parts.append(f"<p>k6 Reason: {_esc(k6['reason'])}</p>")
        breached = _normalize_str_list(diag.get("breached_thresholds"))
        if breached:
            breached_html = ", ".join(f"<code>{_esc(t)}</code>" for t in breached)
            parts.append(f"<p>Breached Thresholds: {breached_html}</p>")
        notes = _normalize_str_list(diag.get("notes"))
        if notes:
            parts.append("<ul>")
            for note in notes:
                parts.append(f"<li>{_esc(note)}</li>")
            parts.append("</ul>")

    parts.append("</section>")
    parts.extend(["</div>", "</body>", "</html>"])
    return "\n".join(parts)
