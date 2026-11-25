"""Quality Gate evaluation engine and Feishu/DingTalk/Slack webhook dispatcher (R5)."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.config import GateConfig, ProjectConfig
from reconqa.models.schemas import (
    ChainRunOut,
    GateRuleResult,
    QualityGateMetrics,
    QualityGateRequest,
    QualityGateResult,
    ScenarioAssertion,
    WebhookDeliveryResult,
)
from reconqa.models.tables import Finding, Project, Run, ScenarioModel
from reconqa.modules.hub.chain_runner import checkpoint_to_out, list_checkpoints
from reconqa.pipeline import _collect_findings
from reconqa.utils.io import read_json

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

_BLOCKED_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "metadata.google.internal",
        "metadata.goog",
    }
)
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_SIMILARITY_SUFFIX_RE = re.compile(r"\s*\(similarity=\d+(?:\.\d+)?\)")
_WS_RE = re.compile(r"\s+")


class GateLookupError(LookupError):
    """Raised when a project, run, or baseline run cannot be resolved."""


def _is_numeric(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_perf_category(category: str) -> bool:
    low = str(category or "").strip().lower()
    return low.startswith("perf_") or low == "performance"


def _safe_read_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = read_json(path)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _normalize_finding_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Finding):
        return {
            "category": str(raw.category or ""),
            "severity": str(raw.severity or "info"),
            "endpoint": str(raw.endpoint or ""),
            "title": str(raw.title or ""),
            "evidence": dict(raw.evidence) if isinstance(raw.evidence, dict) else {},
        }
    if isinstance(raw, dict):
        ev = raw.get("evidence")
        return {
            "category": str(raw.get("category") or ""),
            "severity": str(raw.get("severity") or "info"),
            "endpoint": str(raw.get("endpoint") or ""),
            "title": str(raw.get("title") or raw.get("name") or raw.get("type") or "Finding"),
            "evidence": dict(ev) if isinstance(ev, dict) else {},
        }
    return {
        "category": str(getattr(raw, "category", "") or ""),
        "severity": str(getattr(raw, "severity", "info") or "info"),
        "endpoint": str(getattr(raw, "endpoint", "") or ""),
        "title": str(getattr(raw, "title", "") or ""),
        "evidence": dict(getattr(raw, "evidence", {}) or {}),
    }


def _finding_signature(finding: dict[str, Any]) -> tuple[str, str, str, str]:
    category = str(finding.get("category") or "").strip().lower()
    severity = str(finding.get("severity") or "").strip().lower()
    endpoint = str(finding.get("endpoint") or "").strip().lower()
    raw_title = str(finding.get("title") or "").strip().lower()
    clean_title = _WS_RE.sub(" ", _SIMILARITY_SUFFIX_RE.sub("", raw_title)).strip()
    return (category, severity, endpoint, clean_title)


def extract_run_findings(run_dir: Path | None) -> list[dict[str, Any]]:
    """Load normalized findings from a local run directory."""
    if run_dir is None or not run_dir.is_dir():
        return []
    collected = _collect_findings(run_dir)
    if collected:
        return [_normalize_finding_dict(item) for item in collected]

    summary = _safe_read_json_dict(run_dir / "run_summary.json")
    if summary and isinstance(summary.get("findings"), list):
        return [
            _normalize_finding_dict(item)
            for item in summary["findings"]
            if isinstance(item, dict)
        ]
    return []


def extract_run_coverage_pct(
    run_dir: Path | None,
    *,
    summary_metrics: dict[str, Any] | None = None,
) -> float | None:
    """Extract per-run API coverage percentage (never cumulative inventory)."""
    if run_dir is not None and run_dir.is_dir():
        cov_data = _safe_read_json_dict(run_dir / "hub" / "coverage_diff.json")
        if cov_data:
            for key in ("coverage_pct", "overall_coverage_pct"):
                val = cov_data.get(key)
                if _is_numeric(val):
                    return round(float(val), 2)

    if isinstance(summary_metrics, dict):
        for key in ("coverage_pct", "overall_coverage_pct"):
            val = summary_metrics.get(key)
            if _is_numeric(val):
                return round(float(val), 2)

    if run_dir is not None and run_dir.is_dir():
        run_summary = _safe_read_json_dict(run_dir / "run_summary.json")
        if run_summary and isinstance(run_summary.get("summary_metrics"), dict):
            sm = run_summary["summary_metrics"]
            for key in ("coverage_pct", "overall_coverage_pct"):
                val = sm.get(key)
                if _is_numeric(val):
                    return round(float(val), 2)

        for fname in ("openapi.json", "openapi.yaml"):
            oa_path = run_dir / "hub" / fname
            if not oa_path.is_file():
                continue
            try:
                if fname.endswith(".json"):
                    doc = read_json(oa_path)
                else:
                    doc = yaml.safe_load(oa_path.read_text(encoding="utf-8"))
            except Exception:
                doc = None
            if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
                continue
            total = 0
            live = 0
            for path_item in doc["paths"].values():
                if not isinstance(path_item, dict):
                    continue
                for method, op in path_item.items():
                    if str(method).lower() not in _HTTP_METHODS or not isinstance(op, dict):
                        continue
                    total += 1
                    src = str(op.get("x-discovery-source") or "").strip().lower()
                    srcs = op.get("x-discovery-sources")
                    if src == "har-live" or (
                        isinstance(srcs, list)
                        and any(str(s).strip().lower() == "har-live" for s in srcs)
                    ):
                        live += 1
            if total > 0:
                return round((live / total) * 100.0, 2)

    return None


def extract_run_p95_ms(
    run_dir: Path | None,
) -> tuple[float | None, Literal["k6", "har", "none"]]:
    """Extract P95 latency (ms) from k6_analysis.json falling back to har_perf_report.json."""
    if run_dir is None or not run_dir.is_dir():
        return None, "none"

    k6_data = _safe_read_json_dict(run_dir / "performance" / "k6" / "k6_analysis.json")
    if k6_data:
        k6_status = str(k6_data.get("status") or "").strip().lower()
        if k6_status != "skipped":
            metrics = k6_data.get("metrics") if isinstance(k6_data.get("metrics"), dict) else {}
            summary = k6_data.get("summary") if isinstance(k6_data.get("summary"), dict) else {}
            latency_ms = (
                metrics.get("latency_ms") if isinstance(metrics.get("latency_ms"), dict) else {}
            )
            http_dur = (
                metrics.get("http_req_duration")
                if isinstance(metrics.get("http_req_duration"), dict)
                else {}
            )
            http_dur_vals = (
                http_dur.get("values") if isinstance(http_dur.get("values"), dict) else {}
            )
            candidates = (
                latency_ms.get("p95"),
                summary.get("p95_ms"),
                k6_data.get("p95_ms"),
                http_dur_vals.get("p(95)"),
                http_dur.get("p(95)"),
            )
            for cand in candidates:
                if _is_numeric(cand):
                    return round(float(cand), 2), "k6"

    har_data = _safe_read_json_dict(run_dir / "performance" / "har_perf_report.json")
    if har_data:
        summary = har_data.get("summary") if isinstance(har_data.get("summary"), dict) else {}
        total_reqs = summary.get("total_requests")
        for key in ("p95_total_ms", "p95_ttfb_ms"):
            cand = summary.get(key)
            if _is_numeric(cand):
                val = float(cand)
                if (isinstance(total_reqs, int) and total_reqs > 0) or val > 0.0:
                    return round(val, 2), "har"

    return None, "none"


def _load_offline_checkpoints(
    chain_runs_dir: Path | None,
    *,
    run_id: str | None = None,
) -> tuple[list[ChainRunOut], dict[str, str]]:
    if chain_runs_dir is None or not chain_runs_dir.is_dir():
        return [], {}
    chains: list[ChainRunOut] = []
    priority_hints: dict[str, str] = {}
    for path in sorted(chain_runs_dir.glob("*.json")):
        if not path.is_file():
            continue
        raw = _safe_read_json_dict(path)
        if raw is None:
            continue
        if run_id and raw.get("run_id") and raw.get("run_id") != run_id:
            continue
        try:
            out = checkpoint_to_out(raw)
        except Exception:
            continue
        chains.append(out)
        raw_prio = raw.get("priority")
        if isinstance(raw_prio, str) and raw_prio.strip():
            priority_hints[out.chain_run_id] = raw_prio.strip().upper()
    chains.sort(key=lambda item: (item.updated_at or item.created_at or ""), reverse=True)
    return chains, priority_hints


def _load_offline_scenarios(run_dir: Path | None) -> list[dict[str, Any]]:
    if run_dir is None or not run_dir.is_dir():
        return []
    for cand in (run_dir / "hub" / "scenarios.json", run_dir / "scenarios.json"):
        if not cand.is_file():
            continue
        try:
            data = read_json(cand)
        except Exception:
            continue
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and isinstance(data.get("scenarios"), list):
            return [item for item in data["scenarios"] if isinstance(item, dict)]
    return []


def _parse_model_assertions(row: ScenarioModel) -> list[ScenarioAssertion]:
    try:
        raw_list = json.loads(row.assertions_json or "[]")
    except Exception:
        return []
    if not isinstance(raw_list, list):
        return []
    out: list[ScenarioAssertion] = []
    for item in raw_list:
        if isinstance(item, dict):
            try:
                out.append(ScenarioAssertion.model_validate(item))
            except Exception:
                continue
    return out


def _endpoint_matches_target(method: str, url_or_path: str, target_endpoint: str) -> bool:
    from reconqa.server.routers.scenarios import endpoint_matches_target

    return endpoint_matches_target(method, url_or_path, target_endpoint)


def _step_matches_target(step_method: str, step_url: str, target_endpoint: str) -> bool:
    """Match a persisted ChainStepResult against a ScenarioAssertion target_endpoint.

    chain_runner strips GraphQL `#OperationName` fragments before persisting step.url.
    When step_url has no `#fragment` and target_endpoint has a non-SPA `#OperationName`
    fragment, also match against the base endpoint (without stripping `#/` SPA routes
    or relaxing comparisons when step_url already carries a different `#fragment`).
    """
    if _endpoint_matches_target(step_method, step_url, target_endpoint):
        return True
    raw_target = (target_endpoint or "").strip()
    raw_url = (step_url or "").strip()
    if "#" in raw_target and "#" not in raw_url:
        base_target, raw_frag = raw_target.split("#", 1)
        frag = raw_frag.split("?", 1)[0].strip()
        if frag and not frag.startswith(("/", "!")) and base_target.strip():
            return _endpoint_matches_target(step_method, raw_url, base_target.strip())
    return False


def _extract_scenario_entries(
    scenarios: Sequence[ScenarioModel | dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in scenarios:
        if isinstance(item, ScenarioModel):
            status = str(item.status or "active").strip().lower()
            if status == "archived":
                continue
            entries.append(
                {
                    "slug": str(item.slug or "").strip(),
                    "name": str(item.name or "").strip(),
                    "priority": str(item.priority or "P2").strip().upper(),
                    "assertions": _parse_model_assertions(item),
                }
            )
        elif isinstance(item, dict):
            status = str(item.get("status") or "active").strip().lower()
            if status == "archived":
                continue
            assertions: list[ScenarioAssertion] = []
            for raw_a in item.get("assertions") or []:
                if isinstance(raw_a, ScenarioAssertion):
                    assertions.append(raw_a)
                elif isinstance(raw_a, dict):
                    try:
                        assertions.append(ScenarioAssertion.model_validate(raw_a))
                    except Exception:
                        continue
            entries.append(
                {
                    "slug": str(item.get("slug") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                    "priority": str(item.get("priority") or "P2").strip().upper(),
                    "assertions": assertions,
                }
            )
    return entries


def _resolve_chain_priority(
    chain: ChainRunOut,
    *,
    priority_hints: dict[str, str],
    corr_chain_to_scenario: dict[str, str],
    corr_chain_steps_scenarios: dict[str, list[str]],
    priority_by_key: dict[str, str],
    slug_by_key: dict[str, str],
    scenario_entries: list[dict[str, Any]],
) -> tuple[str | None, set[str]]:
    """Resolve (priority, matched_p0_slugs) for a ChainRunOut checkpoint."""
    matched_p0_slugs: set[str] = set()

    hint = priority_hints.get(chain.chain_run_id)
    if hint and hint != "P0":
        return hint, matched_p0_slugs

    candidate_keys: list[str] = []
    if chain.chain_name in corr_chain_to_scenario:
        candidate_keys.append(corr_chain_to_scenario[chain.chain_name])
    candidate_keys.extend(corr_chain_steps_scenarios.get(chain.chain_name, []))
    candidate_keys.append(chain.chain_name)

    resolved_priorities: list[str] = []
    for raw_key in candidate_keys:
        norm = str(raw_key or "").strip().lower()
        if not norm:
            continue
        if norm in priority_by_key:
            prio = priority_by_key[norm]
            resolved_priorities.append(prio)
            if prio == "P0" and norm in slug_by_key:
                matched_p0_slugs.add(slug_by_key[norm])

    if hint != "P0":
        if "P0" in resolved_priorities:
            return "P0", matched_p0_slugs
        if resolved_priorities:
            return resolved_priorities[0], matched_p0_slugs

    step_priorities: list[str] = []
    for step in chain.step_results:
        for sc in scenario_entries:
            assertions: list[ScenarioAssertion] = sc["assertions"]
            if any(
                a.target_endpoint
                and _step_matches_target(step.method, step.url, a.target_endpoint)
                for a in assertions
            ):
                prio = sc["priority"]
                step_priorities.append(prio)
                if prio == "P0" and sc["slug"]:
                    matched_p0_slugs.add(sc["slug"])

    if hint == "P0":
        return "P0", matched_p0_slugs
    if "P0" in step_priorities:
        return "P0", matched_p0_slugs
    if step_priorities:
        return step_priorities[0], matched_p0_slugs

    return None, matched_p0_slugs


def merge_gate_config(
    base: GateConfig | None = None,
    request: QualityGateRequest | None = None,
) -> GateConfig:
    """Merge GateConfig defaults with non-None QualityGateRequest overrides."""
    cfg = (base or GateConfig()).model_copy(deep=True)
    if request is None:
        return cfg

    if request.max_p0_chain_failures is not None:
        cfg.max_p0_chain_failures = request.max_p0_chain_failures
    if request.max_new_high_security is not None:
        cfg.max_new_high_security = request.max_new_high_security
    if request.max_coverage_drop_pct is not None:
        cfg.max_coverage_drop_pct = request.max_coverage_drop_pct
    if request.max_p95_ms is not None:
        cfg.max_p95_ms = request.max_p95_ms
    if request.fail_on_high_without_baseline is not None:
        cfg.fail_on_high_without_baseline = request.fail_on_high_without_baseline
    if request.feishu_webhook is not None:
        cfg.feishu_webhook = request.feishu_webhook
    if request.dingtalk_webhook is not None:
        cfg.dingtalk_webhook = request.dingtalk_webhook
    if request.slack_webhook is not None:
        cfg.slack_webhook = request.slack_webhook
    if request.webhook_timeout_sec is not None:
        cfg.webhook_timeout_sec = request.webhook_timeout_sec
    return cfg


def evaluate_quality_gate(
    *,
    current_findings: Sequence[dict[str, Any] | Finding] = (),
    baseline_findings: Sequence[dict[str, Any] | Finding] | None = None,
    baseline_available: bool = False,
    current_coverage_pct: float | None = None,
    baseline_coverage_pct: float | None = None,
    p95_ms: float | None = None,
    p95_source: Literal["k6", "har", "none"] = "none",
    chain_runs: Sequence[ChainRunOut] = (),
    chain_priority_hints: dict[str, str] | None = None,
    correlation_chains: Sequence[dict[str, Any]] = (),
    scenarios: Sequence[ScenarioModel | dict[str, Any]] = (),
    gate_config: GateConfig | None = None,
    project_id: str | None = None,
    project_name: str = "",
    target_label: str = "",
    run_id: str | None = None,
    baseline_run_id: str | None = None,
) -> QualityGateResult:
    """Evaluate all 4 Quality Gate rules over normalized project/run inputs."""
    cfg = gate_config or GateConfig()
    hints = chain_priority_hints or {}

    # --- Rule 1: P0 chain / scenario failures ---
    scenario_entries = _extract_scenario_entries(scenarios)
    priority_by_key: dict[str, str] = {}
    slug_by_key: dict[str, str] = {}
    for sc in scenario_entries:
        for key in (sc["slug"], sc["name"]):
            norm = key.strip().lower()
            if norm:
                priority_by_key[norm] = sc["priority"]
                if sc["slug"]:
                    slug_by_key[norm] = sc["slug"]

    corr_chain_to_scenario: dict[str, str] = {}
    corr_chain_steps_scenarios: dict[str, list[str]] = {}
    for ch in correlation_chains:
        if not isinstance(ch, dict):
            continue
        cname = str(ch.get("name") or "").strip()
        csc = str(ch.get("scenario") or "").strip()
        if cname and csc:
            corr_chain_to_scenario[cname] = csc
        if cname and isinstance(ch.get("steps"), list):
            step_scs = [
                str(st.get("scenario") or "").strip()
                for st in ch["steps"]
                if isinstance(st, dict) and str(st.get("scenario") or "").strip()
            ]
            if step_scs:
                corr_chain_steps_scenarios[cname] = step_scs

    p0_failed_chains = 0
    failed_chain_names: list[str] = []
    failed_chain_p0_slugs: set[str] = set()
    for chain in chain_runs:
        prio, matched_slugs = _resolve_chain_priority(
            chain,
            priority_hints=hints,
            corr_chain_to_scenario=corr_chain_to_scenario,
            corr_chain_steps_scenarios=corr_chain_steps_scenarios,
            priority_by_key=priority_by_key,
            slug_by_key=slug_by_key,
            scenario_entries=scenario_entries,
        )
        if chain.status == "failed":
            # Count P0 failed chains and conservatively count unmapped failed chains;
            # explicitly mapped P1/P2/P3 chains are NOT counted as P0 failures.
            if prio == "P0" or prio is None:
                p0_failed_chains += 1
                failed_chain_names.append(chain.chain_name)
                failed_chain_p0_slugs.update(matched_slugs)

    p0_failed_assertions = 0
    unlinked_p0_failed_scenarios = 0
    failed_p0_scenario_slugs: list[str] = []
    for sc in scenario_entries:
        if sc["priority"] != "P0":
            continue
        failed_asserts = [a for a in sc["assertions"] if a.status == "failed"]
        if failed_asserts:
            p0_failed_assertions += len(failed_asserts)
            if sc["slug"] not in failed_chain_p0_slugs:
                unlinked_p0_failed_scenarios += 1
                failed_p0_scenario_slugs.append(sc["slug"] or sc["name"])

    p0_chain_failures = p0_failed_chains + unlinked_p0_failed_scenarios
    rule1_passed = p0_chain_failures <= cfg.max_p0_chain_failures
    rule1_msg = (
        f"P0 failures={p0_chain_failures} "
        f"(failed_chains={p0_failed_chains}, unlinked_failed_p0_scenarios={unlinked_p0_failed_scenarios}, "
        f"failed_assertions={p0_failed_assertions}) <= max {cfg.max_p0_chain_failures}"
        if rule1_passed
        else (
            f"P0 failures={p0_chain_failures} exceeded max {cfg.max_p0_chain_failures} "
            f"(failed_chains={p0_failed_chains}, failed_p0_assertions={p0_failed_assertions})"
        )
    )
    rule1 = GateRuleResult(
        rule="p0_chain_failures",
        label="P0 Chain & Assertion Failures",
        passed=rule1_passed,
        skipped=False,
        actual=p0_chain_failures,
        threshold=cfg.max_p0_chain_failures,
        message=rule1_msg,
        details={
            "p0_failed_chains": p0_failed_chains,
            "p0_failed_assertions": p0_failed_assertions,
            "unlinked_p0_failed_scenarios": unlinked_p0_failed_scenarios,
            "failed_chains": failed_chain_names,
            "failed_p0_scenarios": failed_p0_scenario_slugs,
        },
    )

    # --- Rule 2: New High/Critical Security Findings ---
    norm_cur = [_normalize_finding_dict(f) for f in current_findings]
    cur_high_sec = [
        f
        for f in norm_cur
        if str(f["severity"]).lower() in {"high", "critical"}
        and not _is_perf_category(f["category"])
    ]
    cur_counter = Counter(_finding_signature(f) for f in cur_high_sec)
    high_security_count = sum(cur_counter.values())

    baseline_high_security_count: int | None = None
    if baseline_available:
        norm_base = [_normalize_finding_dict(f) for f in (baseline_findings or ())]
        base_high_sec = [
            f
            for f in norm_base
            if str(f["severity"]).lower() in {"high", "critical"}
            and not _is_perf_category(f["category"])
        ]
        base_counter = Counter(_finding_signature(f) for f in base_high_sec)
        baseline_high_security_count = sum(base_counter.values())
        diff_counter = cur_counter - base_counter
        new_high_security_count = sum(diff_counter.values())
        rule2_skipped = False
        rule2_passed = new_high_security_count <= cfg.max_new_high_security
        rule2_msg = (
            f"New High+ security findings={new_high_security_count} "
            f"(current={high_security_count}, baseline={baseline_high_security_count}) "
            f"{'<=' if rule2_passed else '>'} max {cfg.max_new_high_security}"
        )
    elif cfg.fail_on_high_without_baseline:
        new_high_security_count = high_security_count
        rule2_skipped = False
        rule2_passed = new_high_security_count <= cfg.max_new_high_security
        rule2_msg = (
            f"No baseline available (fail_on_high_without_baseline=True); "
            f"High+ security findings={new_high_security_count} "
            f"{'<=' if rule2_passed else '>'} max {cfg.max_new_high_security}"
        )
    else:
        new_high_security_count = 0
        rule2_skipped = True
        rule2_passed = True
        rule2_msg = (
            f"Skipped: no baseline available (fail_on_high_without_baseline=False); "
            f"current High+ findings={high_security_count}"
        )

    rule2 = GateRuleResult(
        rule="new_high_security",
        label="New High/Critical Security Findings",
        passed=rule2_passed,
        skipped=rule2_skipped,
        actual=new_high_security_count,
        threshold=cfg.max_new_high_security,
        message=rule2_msg,
        details={
            "high_security_count": high_security_count,
            "baseline_high_security_count": baseline_high_security_count,
            "baseline_available": baseline_available,
        },
    )

    # --- Rule 3: Coverage Drop % ---
    if current_coverage_pct is not None and baseline_coverage_pct is not None:
        coverage_drop_pct = round(baseline_coverage_pct - current_coverage_pct, 2)
        rule3_skipped = False
        rule3_passed = coverage_drop_pct <= cfg.max_coverage_drop_pct
        rule3_msg = (
            f"Coverage drop={coverage_drop_pct:.2f}% "
            f"(baseline={baseline_coverage_pct:.2f}% -> current={current_coverage_pct:.2f}%) "
            f"{'<=' if rule3_passed else '>'} max {cfg.max_coverage_drop_pct:.2f}%"
        )
    else:
        coverage_drop_pct = None
        rule3_skipped = True
        rule3_passed = True
        if current_coverage_pct is None and baseline_coverage_pct is None:
            rule3_msg = "Skipped: no current or baseline coverage data available"
        elif baseline_coverage_pct is None:
            rule3_msg = f"Skipped: no baseline coverage data (current={current_coverage_pct:.2f}%)"
        else:
            rule3_msg = f"Skipped: no current coverage data (baseline={baseline_coverage_pct:.2f}%)"

    rule3 = GateRuleResult(
        rule="coverage_drop_pct",
        label="API Coverage Drop %",
        passed=rule3_passed,
        skipped=rule3_skipped,
        actual=coverage_drop_pct,
        threshold=cfg.max_coverage_drop_pct,
        message=rule3_msg,
        details={
            "current_coverage_pct": current_coverage_pct,
            "baseline_coverage_pct": baseline_coverage_pct,
        },
    )

    # --- Rule 4: P95 Latency ms ---
    if cfg.max_p95_ms is None:
        rule4_skipped = True
        rule4_passed = True
        rule4_msg = "Skipped: max_p95_ms threshold is not configured"
    elif p95_ms is None:
        rule4_skipped = True
        rule4_passed = True
        rule4_msg = f"Skipped: no P95 latency data available (threshold={cfg.max_p95_ms:.2f}ms)"
    else:
        rule4_skipped = False
        rule4_passed = p95_ms <= cfg.max_p95_ms
        rule4_msg = (
            f"P95 latency={p95_ms:.2f}ms ({p95_source}) "
            f"{'<=' if rule4_passed else '>'} max {cfg.max_p95_ms:.2f}ms"
        )

    rule4 = GateRuleResult(
        rule="p95_latency_ms",
        label="P95 Latency (ms)",
        passed=rule4_passed,
        skipped=rule4_skipped,
        actual=p95_ms,
        threshold=cfg.max_p95_ms,
        message=rule4_msg,
        details={"source": p95_source},
    )

    rules = [rule1, rule2, rule3, rule4]
    overall_passed = all(r.passed for r in rules)
    failed_rules = [r.rule for r in rules if not r.passed]
    evaluated_count = sum(1 for r in rules if not r.skipped)

    if overall_passed:
        summary = f"PASSED: all {evaluated_count} active quality gate rules satisfied"
    else:
        summary = f"FAILED: {len(failed_rules)} rule(s) breached ({', '.join(failed_rules)})"

    metrics = QualityGateMetrics(
        p0_chain_failures=p0_chain_failures,
        p0_failed_chains=p0_failed_chains,
        p0_failed_assertions=p0_failed_assertions,
        total_chain_runs=len(chain_runs),
        high_security_count=high_security_count,
        baseline_high_security_count=baseline_high_security_count,
        new_high_security_count=new_high_security_count,
        current_coverage_pct=current_coverage_pct,
        baseline_coverage_pct=baseline_coverage_pct,
        coverage_drop_pct=coverage_drop_pct,
        p95_ms=p95_ms,
        p95_source=p95_source,
        baseline_available=baseline_available,
    )

    resolved_label = target_label or project_name or (run_id or "offline-run")
    return QualityGateResult(
        project_id=project_id,
        project_name=project_name,
        target_label=resolved_label,
        run_id=run_id,
        baseline_run_id=baseline_run_id,
        passed=overall_passed,
        status="passed" if overall_passed else "failed",
        evaluated_at=datetime.now(UTC).isoformat(),
        rules=rules,
        metrics=metrics,
        summary=summary,
        webhook_results=[],
    )


def evaluate_run_dir_gate(
    run_dir: Path,
    *,
    baseline_dir: Path | None = None,
    chain_runs_dir: Path | None = None,
    run_id: str | None = None,
    baseline_run_id: str | None = None,
    gate_config: GateConfig | None = None,
    project_name: str = "",
    target_label: str = "",
) -> QualityGateResult:
    """Evaluate quality gate rules offline from a local run directory."""
    cur_findings = extract_run_findings(run_dir)
    base_findings = extract_run_findings(baseline_dir) if baseline_dir is not None else None
    cur_cov = extract_run_coverage_pct(run_dir)
    base_cov = extract_run_coverage_pct(baseline_dir) if baseline_dir is not None else None
    p95_ms, p95_source = extract_run_p95_ms(run_dir)

    corr_data = _safe_read_json_dict(run_dir / "hub" / "correlation.json") or {}
    corr_chains = [ch for ch in (corr_data.get("chains") or []) if isinstance(ch, dict)]

    resolved_chain_dir = chain_runs_dir or (
        run_dir / "chain_runs" if (run_dir / "chain_runs").is_dir() else None
    )
    chain_runs, priority_hints = _load_offline_checkpoints(resolved_chain_dir, run_id=run_id)
    scenarios = _load_offline_scenarios(run_dir)

    return evaluate_quality_gate(
        current_findings=cur_findings,
        baseline_findings=base_findings,
        baseline_available=baseline_dir is not None,
        current_coverage_pct=cur_cov,
        baseline_coverage_pct=base_cov,
        p95_ms=p95_ms,
        p95_source=p95_source,
        chain_runs=chain_runs,
        chain_priority_hints=priority_hints,
        correlation_chains=corr_chains,
        scenarios=scenarios,
        gate_config=gate_config,
        project_id=None,
        project_name=project_name,
        target_label=target_label or project_name or run_dir.name,
        run_id=run_id,
        baseline_run_id=baseline_run_id,
    )


evaluate_offline_quality_gate = evaluate_run_dir_gate


async def _load_run_findings_db(db: AsyncSession, run_obj: Run) -> list[dict[str, Any]]:
    f_res = await db.execute(select(Finding).where(Finding.run_id == run_obj.id))
    rows = list(f_res.scalars().all())
    if rows:
        return [_normalize_finding_dict(r) for r in rows]
    ws = Path(run_obj.workspace_dir) if run_obj.workspace_dir else None
    if ws is not None and ws.is_dir():
        return extract_run_findings(ws)
    return []


async def evaluate_project_quality_gate_db(
    db: AsyncSession,
    project_id: str,
    request: QualityGateRequest | None = None,
    project_config: ProjectConfig | None = None,
) -> tuple[QualityGateResult, GateConfig]:
    """Evaluate quality gate rules for a persisted project and return (result, effective_config)."""
    req = request or QualityGateRequest()
    effective_cfg = merge_gate_config(project_config.gate if project_config else None, req)

    project = await db.get(Project, project_id)
    if project is None:
        raise GateLookupError(f"Project '{project_id}' not found")

    all_runs_res = await db.execute(
        select(Run)
        .where(Run.project_id == project_id)
        .order_by(Run.finished_at.desc(), Run.started_at.desc())
    )
    all_runs = list(all_runs_res.scalars().all())

    selected_run: Run | None = None
    if req.run_id is not None:
        selected_run = await db.get(Run, req.run_id)
        if selected_run is None or selected_run.project_id != project_id:
            raise GateLookupError(f"Run '{req.run_id}' not found in project '{project_id}'")
    else:
        for r in all_runs:
            if r.status == "completed":
                selected_run = r
                break
        if selected_run is None and all_runs:
            selected_run = all_runs[0]

    baseline_run: Run | None = None
    if req.baseline_run_id is not None:
        baseline_run = await db.get(Run, req.baseline_run_id)
        if baseline_run is None or baseline_run.project_id != project_id:
            raise GateLookupError(
                f"Baseline run '{req.baseline_run_id}' not found in project '{project_id}'"
            )
    elif selected_run is not None:
        sel_ts = selected_run.finished_at or selected_run.started_at
        completed_others = [
            r for r in all_runs if r.status == "completed" and r.id != selected_run.id
        ]
        if sel_ts is not None:
            earlier = [
                r
                for r in completed_others
                if (r.finished_at or r.started_at) is not None
                and (r.finished_at or r.started_at) <= sel_ts
            ]
            baseline_run = earlier[0] if earlier else (completed_others[0] if completed_others else None)
        elif completed_others:
            baseline_run = completed_others[0]

    sc_rows = list(
        (await db.execute(select(ScenarioModel).where(ScenarioModel.project_id == project_id)))
        .scalars()
        .all()
    )

    if req.run_id is not None:
        chain_runs = list_checkpoints(project_id, run_id=req.run_id, limit=200)
    else:
        all_cps = list_checkpoints(project_id, run_id=None, limit=200)
        if selected_run is not None:
            chain_runs = [
                cp for cp in all_cps if cp.run_id == selected_run.id or cp.run_id is None
            ]
        else:
            chain_runs = all_cps

    cur_findings: list[dict[str, Any]] = []
    cur_cov: float | None = None
    p95_ms: float | None = None
    p95_source: Literal["k6", "har", "none"] = "none"
    corr_chains: list[dict[str, Any]] = []

    if selected_run is not None:
        cur_findings = await _load_run_findings_db(db, selected_run)
        sel_ws = Path(selected_run.workspace_dir) if selected_run.workspace_dir else None
        cur_cov = extract_run_coverage_pct(
            sel_ws,
            summary_metrics=selected_run.summary_metrics
            if isinstance(selected_run.summary_metrics, dict)
            else None,
        )
        p95_ms, p95_source = extract_run_p95_ms(sel_ws)
        if sel_ws is not None:
            corr_data = _safe_read_json_dict(sel_ws / "hub" / "correlation.json") or {}
            corr_chains = [ch for ch in (corr_data.get("chains") or []) if isinstance(ch, dict)]

    base_findings: list[dict[str, Any]] | None = None
    base_cov: float | None = None
    if baseline_run is not None:
        base_findings = await _load_run_findings_db(db, baseline_run)
        base_ws = Path(baseline_run.workspace_dir) if baseline_run.workspace_dir else None
        base_cov = extract_run_coverage_pct(
            base_ws,
            summary_metrics=baseline_run.summary_metrics
            if isinstance(baseline_run.summary_metrics, dict)
            else None,
        )

    result = evaluate_quality_gate(
        current_findings=cur_findings,
        baseline_findings=base_findings,
        baseline_available=baseline_run is not None,
        current_coverage_pct=cur_cov,
        baseline_coverage_pct=base_cov,
        p95_ms=p95_ms,
        p95_source=p95_source,
        chain_runs=chain_runs,
        correlation_chains=corr_chains,
        scenarios=sc_rows,
        gate_config=effective_cfg,
        project_id=project.id,
        project_name=project.name,
        target_label=project.name,
        run_id=selected_run.id if selected_run is not None else None,
        baseline_run_id=baseline_run.id if baseline_run is not None else None,
    )
    return result, effective_cfg


# --- Webhook URL Validation, Masking, Formatting & Delivery ---


def _is_loopback_host(host: str) -> bool:
    norm = (host or "").strip().lower().rstrip(".")
    if norm in _LOOPBACK_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(norm)
    except ValueError:
        return False
    return ip.is_loopback


def validate_webhook_url(url: str) -> tuple[bool, str | None]:
    """Validate webhook URL: http/https only, no control chars, SSRF-safe (loopback allowed)."""
    raw = (url or "").strip()
    if not raw:
        return False, "Webhook URL is empty"
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False, "Webhook URL contains whitespace or control characters"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        return False, f"Malformed webhook URL: {exc}"

    if parts.scheme.lower() not in {"http", "https"}:
        return False, f"Unsupported URL scheme '{parts.scheme or 'none'}'; only http/https allowed"
    if not parts.netloc or not parts.hostname:
        return False, "Webhook URL is missing a valid host"

    try:
        port = parts.port
    except ValueError as exc:
        return False, f"Malformed webhook URL port: {exc}"
    if port is not None and not (1 <= port <= 65535):
        return False, f"Webhook URL port {port} is out of range (1..65535)"

    host = parts.hostname.strip().lower().rstrip(".")
    if host in _BLOCKED_METADATA_HOSTS:
        return False, f"Blocked cloud metadata host '{host}'"

    if not _is_loopback_host(host):
        try:
            ip = ipaddress.ip_address(host)
            if isinstance(ip, ipaddress.IPv4Address) and ip in _LINK_LOCAL_V4:
                return False, f"Blocked link-local IP address '{host}'"
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                mapped = ip.ipv4_mapped
                if mapped in _LINK_LOCAL_V4 or str(mapped) in _BLOCKED_METADATA_HOSTS:
                    return False, f"Blocked link-local IPv4-mapped address '{host}'"
            if ip.is_link_local:
                return False, f"Blocked link-local IP address '{host}'"
        except ValueError:
            try:
                packed = socket.inet_aton(host)
            except OSError:
                packed = None
            if packed is not None:
                canonical_ip = ipaddress.IPv4Address(packed)
                if host != str(canonical_ip):
                    return False, f"Blocked non-canonical IPv4 literal '{host}'"
                if canonical_ip in _LINK_LOCAL_V4:
                    return False, f"Blocked link-local IP address '{host}'"

    return True, None


def mask_webhook_url(url: str) -> str:
    """Redact userinfo, query parameter values, and trailing secret path tokens."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "[invalid-url]"

    if not parts.scheme or not parts.netloc:
        return "[invalid-url]"

    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        host_repr = f"[{hostname}]"
    else:
        host_repr = hostname
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host_repr}:{port}" if port is not None else host_repr

    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) >= 2:
        segments[-1] = "***"
        masked_path = "/" + "/".join(segments)
    elif len(segments) == 1:
        masked_path = "/***"
    else:
        masked_path = parts.path

    masked_query = ""
    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if pairs:
            masked_query = urlencode([(k, "***") for k, _ in pairs])
        else:
            masked_query = "***"

    return urlunsplit((parts.scheme.lower(), netloc, masked_path, masked_query, ""))


def _build_webhook_title_and_markdown(result: QualityGateResult) -> tuple[str, str]:
    status_tag = "PASS" if result.passed else "FAIL"
    label = result.target_label or result.project_name or result.run_id or "ReconQA"
    title = f"[{status_tag}] ReconQA Quality Gate: {label}"

    lines = [
        f"**Status**: {result.status.upper()} — {result.summary}",
    ]
    if result.project_id:
        lines.append(f"**Project**: `{result.project_name or result.project_id}` (`{result.project_id}`)")
    if result.run_id:
        lines.append(f"**Run ID**: `{result.run_id}`")
    if result.baseline_run_id:
        lines.append(f"**Baseline Run**: `{result.baseline_run_id}`")
    lines.append("")
    lines.append("**Rules**:")
    for rule in result.rules:
        badge = "SKIP" if rule.skipped else ("PASS" if rule.passed else "FAIL")
        lines.append(f"- **{rule.label}** [{badge}]: {rule.message}")
    return title, "\n".join(lines)


def format_webhook_payload(
    provider: Literal["feishu", "dingtalk", "slack"],
    result: QualityGateResult,
) -> dict[str, Any]:
    """Format QualityGateResult into Feishu, DingTalk, or Slack webhook JSON."""
    title, md_body = _build_webhook_title_and_markdown(result)
    if provider == "feishu":
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "green" if result.passed else "red",
                },
                "elements": [
                    {"tag": "markdown", "content": md_body},
                ],
            },
        }
    if provider == "dingtalk":
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n{md_body}",
            },
        }
    if provider == "slack":
        return {
            "text": f"{title} — {result.summary}",
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": title[:150]},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": md_body},
                },
            ],
        }
    raise ValueError(f"Unsupported webhook provider: {provider}")


async def dispatch_webhooks(
    webhooks: dict[str, str | None],
    result: QualityGateResult,
    *,
    timeout_sec: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[WebhookDeliveryResult]:
    """Validate and dispatch configured webhook notifications without raising on errors."""
    deliveries: list[WebhookDeliveryResult] = []
    for raw_provider, raw_url in webhooks.items():
        if raw_url is None or raw_url == "":
            continue
        provider: Literal["feishu", "dingtalk", "slack"] = raw_provider  # type: ignore[assignment]
        masked = mask_webhook_url(raw_url)
        valid, err = validate_webhook_url(raw_url)
        if not valid:
            deliveries.append(
                WebhookDeliveryResult(
                    provider=provider,
                    url_masked=masked,
                    sent=False,
                    status_code=None,
                    error=err,
                )
            )
            continue

        clean_url = raw_url.strip()
        host = (urlsplit(clean_url).hostname or "").strip().lower().rstrip(".")
        is_loopback = _is_loopback_host(host)
        payload = format_webhook_payload(provider, result)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_sec),
                follow_redirects=False,
                trust_env=not is_loopback,
                transport=transport,
            ) as client:
                resp = await client.post(
                    clean_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            if 200 <= resp.status_code < 300:
                deliveries.append(
                    WebhookDeliveryResult(
                        provider=provider,
                        url_masked=masked,
                        sent=True,
                        status_code=resp.status_code,
                        error=None,
                    )
                )
            else:
                deliveries.append(
                    WebhookDeliveryResult(
                        provider=provider,
                        url_masked=masked,
                        sent=False,
                        status_code=resp.status_code,
                        error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                )
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            deliveries.append(
                WebhookDeliveryResult(
                    provider=provider,
                    url_masked=masked,
                    sent=False,
                    status_code=None,
                    error=str(exc)[:300],
                )
            )
    return deliveries


dispatch_gate_webhooks = dispatch_webhooks
