"""Deterministic Business Capability Map (Resource x Action x Role x State) synthesizer."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from reconqa.utils.io import slugify

_INFIX_SEGMENTS = {
    "plf",
    "platform",
    "p",
    "sys",
    "seller",
    "pharmacy",
    "shop",
    "doctor",
    "doc",
    "assistant",
    "yz",
    "patient",
    "m",
    "api",
    "base",
    "db",
    "org",
    "pc",
    "ct",
    "v1",
    "v2",
}

_ROLE_BY_TOKEN: dict[str, str] = {
    "plf": "admin",
    "platform": "admin",
    "admin": "admin",
    "sys": "admin",
    "ops": "admin",
    "manage": "admin",
    "p": "admin",
    "seller": "pharmacy",
    "pharmacy": "pharmacy",
    "medicine": "pharmacy",
    "merchant": "pharmacy",
    "store": "pharmacy",
    "shop": "pharmacy",
    "doctor": "doctor",
    "doc": "doctor",
    "assistant": "assistant",
    "yz": "assistant",
    "patient": "patient",
    "m": "patient",
    "h5": "patient",
}

_EXPLICIT_ROLE_TOKENS = {
    "plf",
    "platform",
    "admin",
    "seller",
    "pharmacy",
    "medicine",
    "doctor",
    "doc",
    "assistant",
    "yz",
    "patient",
}

_ACTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("audit", ("audit", "approve", "reject", "review", "verify", "check")),
    ("refund", ("refund", "cancel", "return", "revoke", "chargeback")),
    ("export", ("export", "download", "excel", "csv", "report")),
    ("delete", ("delete", "del", "remove", "destroy")),
    ("create", ("create", "add", "save", "submit", "new", "insert", "register", "apply")),
    ("update", ("update", "edit", "modify", "set", "enable", "disable", "toggle", "change")),
    ("detail", ("detail", "info", "view", "profile", "get")),
    ("list", ("list", "page", "search", "query", "find", "all", "tree", "count", "stats", "summary")),
)

_STATUS_FIELD_NAMES = {
    "status",
    "orderstatus",
    "auditstatus",
    "checkstatus",
    "constatus",
    "order_status",
    "audit_status",
    "check_status",
    "con_status",
}

_CORE_TRANSACTIONAL_RESOURCES = {
    "order",
    "orders",
    "pres",
    "prescription",
    "con",
    "consult",
    "consultation",
    "bill",
    "billing",
    "pay",
    "payment",
    "refund",
    "trade",
    "checkout",
    "settlement",
    "invoice",
    "wallet",
    "finance",
}

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ROLE_ORDER = {"admin": 0, "pharmacy": 1, "doctor": 2, "assistant": 3, "patient": 4, "common": 5}
_SLOT_PRECEDENCE = {"failed": 3, "covered": 2, "pending": 1, "missing": 0}


def _natural_sort_key(value: str) -> list[Any]:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", str(value or ""))]


def _is_param_segment(seg: str) -> bool:
    return (seg.startswith("{") and seg.endswith("}")) or seg.isdigit()


def _normalize_endpoint_parts(path: str, host: str = "") -> tuple[str, str, str]:
    """Return (module, resource, inferred_role) for an API path."""
    raw_segs = [s.strip() for s in (path or "/").split("/") if s.strip()]
    low_segs = [s.lower() for s in raw_segs]
    non_param = [s for s in low_segs if not _is_param_segment(s)]

    if not non_param:
        return "root", "root", "common"

    module = non_param[0]
    resource = ""
    for seg in non_param[1:]:
        if seg not in _INFIX_SEGMENTS:
            resource = seg
            break
    if not resource:
        resource = module

    # Infer role from path infixes (prefer explicit role tokens; allow short tokens like p/m/shop after module)
    inferred_role = "common"
    for idx, seg in enumerate(non_param):
        if seg in _EXPLICIT_ROLE_TOKENS:
            inferred_role = _ROLE_BY_TOKEN[seg]
            break
        if idx > 0 and seg in _ROLE_BY_TOKEN:
            inferred_role = _ROLE_BY_TOKEN[seg]
            break

    if inferred_role == "common" and host:
        h_low = host.strip().lower()
        first_host_lbl = h_low.split(".", 1)[0]
        for tok, role_name in (
            ("admin", "admin"),
            ("medicine", "pharmacy"),
            ("pharmacy", "pharmacy"),
            ("doctor", "doctor"),
            ("yz", "assistant"),
            ("assistant", "assistant"),
            ("patient", "patient"),
        ):
            if first_host_lbl == tok or first_host_lbl.startswith(f"{tok}-"):
                inferred_role = role_name
                break

    return module, resource, inferred_role


def _classify_action(method: str, path: str) -> str:
    m_up = (method or "GET").upper().strip()
    raw_segs = [s.strip().lower() for s in (path or "/").split("/") if s.strip()]
    non_param = [s for s in raw_segs if not _is_param_segment(s)]

    # Inspect tail segments from most specific (last) backwards
    tail_segs = list(reversed(non_param[-3:])) if non_param else []
    for seg in tail_segs:
        tokens = [t for t in re.split(r"[^a-z0-9]+", seg) if t]
        for action, keywords in _ACTION_KEYWORDS:
            if seg in keywords or any(t in keywords for t in tokens):
                return action
            if any(seg.startswith(kw) or seg.endswith(kw) for kw in keywords if len(kw) >= 4):
                return action

    if m_up == "DELETE":
        return "delete"
    if m_up in {"PUT", "PATCH"}:
        return "update"
    if m_up == "GET" and raw_segs and _is_param_segment(raw_segs[-1]):
        return "detail"
    return "read" if m_up in {"GET", "HEAD", "OPTIONS"} else "write"


def _mine_status_enums(
    payload: Any,
    out: dict[str, set[str]],
    *,
    depth: int = 0,
    max_depth: int = 4,
    max_vals: int = 12,
) -> None:
    if depth > max_depth or payload is None:
        return
    if isinstance(payload, dict):
        for k, v in payload.items():
            k_str = str(k)
            k_low = k_str.lower()
            if k_low in _STATUS_FIELD_NAMES and not isinstance(v, (dict, list, bool)) and v is not None:
                s_val = str(v).strip()
                if s_val and len(s_val) <= 32:
                    bucket = out.setdefault(k_str, set())
                    if len(bucket) < max_vals:
                        bucket.add(s_val)
            elif isinstance(v, (dict, list)):
                _mine_status_enums(v, out, depth=depth + 1, max_depth=max_depth, max_vals=max_vals)
    elif isinstance(payload, list):
        for item in payload[:12]:
            _mine_status_enums(item, out, depth=depth + 1, max_depth=max_depth, max_vals=max_vals)


def _normalize_scenario_obj(sc: Any) -> dict[str, Any]:
    """Normalize either a ScenarioModel ORM row or ScenarioResponse/dict into a uniform dict."""
    if isinstance(sc, dict):
        return {
            "id": str(sc.get("id") or ""),
            "slug": str(sc.get("slug") or ""),
            "name": str(sc.get("name") or ""),
            "priority": str(sc.get("priority") or "P2"),
            "status": str(sc.get("status") or "active"),
            "business_domain": str(sc.get("business_domain") or ""),
            "effectiveness_level": str(sc.get("effectiveness_level") or "L0"),
            "assertions": list(sc.get("assertions") or []),
            "negative_cases": list(sc.get("negative_cases") or []),
        }
    if hasattr(sc, "assertions_json"):
        try:
            assertions = json.loads(sc.assertions_json or "[]")
        except Exception:
            assertions = []
        try:
            neg_cases = json.loads(sc.negative_cases_json or "[]")
        except Exception:
            neg_cases = []
        return {
            "id": str(sc.id),
            "slug": str(sc.slug),
            "name": str(sc.name),
            "priority": str(sc.priority or "P2"),
            "status": str(sc.status or "active"),
            "business_domain": str(sc.business_domain or ""),
            "effectiveness_level": str(getattr(sc, "effectiveness_level", "L0") or "L0"),
            "assertions": assertions if isinstance(assertions, list) else [],
            "negative_cases": neg_cases if isinstance(neg_cases, list) else [],
        }
    if hasattr(sc, "model_dump"):
        return _normalize_scenario_obj(sc.model_dump())
    return {}


def _extract_target_path(target_endpoint: str) -> str:
    raw = (target_endpoint or "").strip()
    if not raw:
        return ""
    parts = raw.split(None, 1)
    p = parts[1] if len(parts) == 2 else raw
    return p.split("?", 1)[0].strip().rstrip("/").lower()


def _best_slot_status(statuses: list[str]) -> str:
    best = "missing"
    for st in statuses:
        norm = st if st in _SLOT_PRECEDENCE else "pending"
        if _SLOT_PRECEDENCE[norm] > _SLOT_PRECEDENCE[best]:
            best = norm
    return best


def build_capability_map(
    inventory: dict[str, Any],
    scenarios: list[Any],
    project_id: str,
    project_name: str,
) -> dict[str, Any]:
    """Group flat API endpoints into normalized Business Capabilities (Resource x Action x Role x State)."""
    endpoints = list(inventory.get("endpoints") or [])
    norm_scenarios = [
        ns
        for ns in (_normalize_scenario_obj(s) for s in (scenarios or []))
        if ns and ns.get("status") != "archived"
    ]

    # Pre-index scenario target paths for fast linkage
    scenario_paths: dict[str, set[str]] = {}
    for sc in norm_scenarios:
        p_set: set[str] = set()
        for a in sc.get("assertions") or []:
            if isinstance(a, dict):
                tp = _extract_target_path(str(a.get("target_endpoint") or ""))
                if tp:
                    p_set.add(tp)
        scenario_paths[sc["id"]] = p_set

    # 1. Group endpoints into capabilities keyed by "{module}/{resource}"
    cap_buckets: dict[str, dict[str, Any]] = {}
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        method = str(ep.get("method") or "GET").upper().strip()
        path = str(ep.get("path") or "/")
        host = str(ep.get("host") or "")
        source = str(ep.get("discovery_source") or "js-static")
        is_live = source == "har-live"
        is_write = method in _WRITE_METHODS

        module, resource, role = _normalize_endpoint_parts(path, host=host)
        action = _classify_action(method, path)
        key = f"{module}/{resource}"

        bucket = cap_buckets.get(key)
        if bucket is None:
            bucket = {
                "key": key,
                "module": module,
                "resource": resource,
                "display_name": f"{module} / {resource}",
                "total_endpoints": 0,
                "live_endpoints": 0,
                "write_endpoints": 0,
                "read_endpoints": 0,
                "roles": set(),
                "actions": set(),
                "status_enums": {},
                "ep_paths_low": set(),
                "ep_scenario_slugs": set(),
                "endpoints": [],
            }
            cap_buckets[key] = bucket

        bucket["total_endpoints"] += 1
        if is_live:
            bucket["live_endpoints"] += 1
            _mine_status_enums(ep.get("response_example"), bucket["status_enums"])
        if is_write:
            bucket["write_endpoints"] += 1
        else:
            bucket["read_endpoints"] += 1

        bucket["roles"].add(role)
        bucket["actions"].add(action)
        clean_p = path.strip().rstrip("/").lower()
        if clean_p:
            bucket["ep_paths_low"].add(clean_p)
        for s_slug in ep.get("scenarios") or []:
            if str(s_slug).strip():
                bucket["ep_scenario_slugs"].add(str(s_slug).strip())

        bucket["endpoints"].append(
            {
                "method": method,
                "path": path,
                "action": action,
                "role": role,
                "discovery_source": source,
                "host": host,
            }
        )

    # 2. Compute priority, scenario linkage, and 5-slot case matrix per capability
    capabilities_out: list[dict[str, Any]] = []
    module_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"capabilities": 0, "live_capabilities": 0, "endpoints": 0}
    )

    for key, b in cap_buckets.items():
        total_ep = b["total_endpoints"]
        live_ep = b["live_endpoints"]
        write_ep = b["write_endpoints"]
        read_ep = b["read_endpoints"]
        live_pct = round((live_ep / total_ep) * 100.0, 2) if total_ep else 0.0

        roles_sorted = sorted(b["roles"], key=lambda r: (_ROLE_ORDER.get(r, 9), r))
        non_common_roles = [r for r in roles_sorted if r != "common"]
        actions_sorted = sorted(b["actions"])
        observed_statuses = {
            fname: sorted(vals, key=_natural_sort_key)
            for fname, vals in sorted(b["status_enums"].items())
            if vals
        }
        has_state_machine = bool(observed_statuses)
        is_core_resource = b["resource"] in _CORE_TRANSACTIONAL_RESOURCES

        priority_score = round(
            live_ep * 3.0
            + write_ep * 2.5
            + len(non_common_roles) * 5.0
            + (8.0 if has_state_machine else 0.0)
            + (10.0 if is_core_resource else 0.0),
            1,
        )
        if (
            (live_ep > 0 and write_ep > 0 and len(non_common_roles) >= 2)
            or (live_ep > 0 and is_core_resource)
            or (live_ep > 0 and write_ep > 0 and has_state_machine)
        ):
            rec_priority = "P0"
        elif (live_ep > 0 and write_ep > 0) or len(non_common_roles) >= 2 or (is_core_resource and write_ep > 0):
            rec_priority = "P1"
        elif live_ep > 0 or write_ep > 0:
            rec_priority = "P2"
        else:
            rec_priority = "P3"

        # Link persisted scenarios
        cap_slug = f"cap_{slugify(key)}"
        linked_sc_objs: list[dict[str, Any]] = []
        for sc in norm_scenarios:
            sc_paths = scenario_paths.get(sc["id"], set())
            path_overlap = bool(
                sc_paths
                and any(
                    sp in b["ep_paths_low"]
                    or any(ep_p.endswith(sp) for ep_p in b["ep_paths_low"] if len(sp) > 1)
                    for sp in sc_paths
                )
            )
            slug_match = sc["slug"] == cap_slug or sc["slug"] in b["ep_scenario_slugs"]
            if slug_match or path_overlap:
                linked_sc_objs.append(sc)

        linked_refs = [
            {
                "id": sc["id"],
                "slug": sc["slug"],
                "name": sc["name"],
                "priority": sc["priority"],
                "effectiveness_level": sc["effectiveness_level"],
            }
            for sc in linked_sc_objs
        ]

        # Collect assertions & negative cases across linked scenarios
        all_assertions = [
            a for sc in linked_sc_objs for a in (sc.get("assertions") or []) if isinstance(a, dict)
        ]
        all_neg_cases = [
            nc
            for sc in linked_sc_objs
            for nc in (sc.get("negative_cases") or [])
            if isinstance(nc, dict)
        ]

        # Slot 1: happy_path
        assertion_statuses = [str(a.get("status") or "pending").lower() for a in all_assertions]
        if "failed" in assertion_statuses:
            hp_status = "failed"
        elif live_ep > 0 and len(all_assertions) >= 1:
            hp_status = "covered"
        elif live_ep > 0:
            hp_status = "pending"
        else:
            hp_status = "missing"

        # Slot 2: invalid_params (validation / boundary)
        inv_cases = [nc for nc in all_neg_cases if str(nc.get("category") or "") in {"validation", "boundary"}]
        inv_status = _best_slot_status([str(nc.get("status") or "pending") for nc in inv_cases])

        # Slot 3: illegal_state (state_machine)
        st_cases = [nc for nc in all_neg_cases if str(nc.get("category") or "") == "state_machine"]
        st_status = _best_slot_status([str(nc.get("status") or "pending") for nc in st_cases])

        # Slot 4: authz_role (auth / security)
        auth_cases = [nc for nc in all_neg_cases if str(nc.get("category") or "") in {"auth", "security"}]
        auth_status = _best_slot_status([str(nc.get("status") or "pending") for nc in auth_cases])

        # Slot 5: consistency_idempotency (idempotency neg_cases OR custom/multi invariant assertions)
        idem_cases = [nc for nc in all_neg_cases if str(nc.get("category") or "") == "idempotency"]
        idem_statuses = [str(nc.get("status") or "pending") for nc in idem_cases]
        custom_assertions = [a for a in all_assertions if str(a.get("operator") or "") == "custom"]
        if custom_assertions or len(all_assertions) >= 2:
            for a in (custom_assertions or all_assertions):
                a_st = str(a.get("status") or "pending")
                idem_statuses.append("covered" if a_st == "passed" else a_st)
        idem_status = _best_slot_status(idem_statuses)
        idem_count = len(idem_cases) + len(custom_assertions)

        case_matrix = [
            {"slot": "happy_path", "status": hp_status, "count": len(all_assertions) or (1 if live_ep > 0 else 0)},
            {"slot": "invalid_params", "status": inv_status, "count": len(inv_cases)},
            {"slot": "illegal_state", "status": st_status, "count": len(st_cases)},
            {"slot": "authz_role", "status": auth_status, "count": len(auth_cases)},
            {"slot": "consistency_idempotency", "status": idem_status, "count": idem_count},
        ]
        tested_slots = sum(1 for slot in case_matrix if slot["status"] in {"covered", "failed"})
        case_coverage_pct = round((tested_slots / 5.0) * 100.0, 1)

        # Sort endpoints inside capability: har-live first, then writes, then path
        sorted_eps = sorted(
            b["endpoints"],
            key=lambda e: (
                0 if e["discovery_source"] == "har-live" else 1,
                0 if e["method"] in _WRITE_METHODS else 1,
                e["path"],
                e["method"],
            ),
        )

        capabilities_out.append(
            {
                "key": key,
                "module": b["module"],
                "resource": b["resource"],
                "display_name": b["display_name"],
                "total_endpoints": total_ep,
                "live_endpoints": live_ep,
                "write_endpoints": write_ep,
                "read_endpoints": read_ep,
                "live_pct": live_pct,
                "roles": roles_sorted,
                "actions": actions_sorted,
                "observed_statuses": observed_statuses,
                "has_state_machine": has_state_machine,
                "priority_score": priority_score,
                "recommended_priority": rec_priority,
                "linked_scenarios": linked_refs,
                "case_matrix": case_matrix,
                "case_coverage_pct": case_coverage_pct,
                "endpoints": sorted_eps,
            }
        )

        m_stat = module_stats[b["module"]]
        m_stat["capabilities"] += 1
        if live_ep > 0:
            m_stat["live_capabilities"] += 1
        m_stat["endpoints"] += total_ep

    capabilities_out.sort(
        key=lambda c: (-c["live_endpoints"], -c["priority_score"], -c["total_endpoints"], c["key"])
    )

    modules_out = [
        {
            "module": mod,
            "capabilities": st["capabilities"],
            "live_capabilities": st["live_capabilities"],
            "endpoints": st["endpoints"],
        }
        for mod, st in sorted(
            module_stats.items(),
            key=lambda kv: (-kv[1]["live_capabilities"], -kv[1]["endpoints"], kv[0]),
        )
    ]

    # 3. Compute 5-Dimensional Quality Metrics
    total_endpoints = len(endpoints)
    total_capabilities = len(capabilities_out)
    live_capabilities = sum(1 for c in capabilities_out if c["live_endpoints"] > 0)
    target_capabilities = (
        max(min(30, live_capabilities), 1)
        if live_capabilities > 0
        else min(30, total_capabilities)
    )
    capability_coverage_pct = (
        round((live_capabilities / total_capabilities) * 100.0, 2) if total_capabilities else 0.0
    )

    stateful_caps = [c for c in capabilities_out if c["has_state_machine"]]
    stateful_capabilities = len(stateful_caps)
    state_transition_covered = sum(
        1
        for c in stateful_caps
        if any(
            s["slot"] == "illegal_state" and s["status"] in {"covered", "failed"}
            for s in c["case_matrix"]
        )
    )
    state_coverage_pct = (
        round((state_transition_covered / stateful_capabilities) * 100.0, 2)
        if stateful_capabilities
        else 0.0
    )

    scope_caps = [c for c in capabilities_out if c["live_endpoints"] > 0] or capabilities_out
    role_matrix_cells_total = sum(max(1, len(c["roles"])) for c in scope_caps)
    role_matrix_cells_covered = sum(
        max(1, len(c["roles"]))
        for c in scope_caps
        if any(
            s["slot"] == "authz_role" and s["status"] in {"covered", "failed"}
            for s in c["case_matrix"]
        )
    )
    role_matrix_coverage_pct = (
        round((role_matrix_cells_covered / role_matrix_cells_total) * 100.0, 2)
        if role_matrix_cells_total
        else 0.0
    )

    negative_slots_total = len(scope_caps) * 4
    negative_slots_tested = sum(
        1
        for c in scope_caps
        for s in c["case_matrix"]
        if s["slot"] != "happy_path" and s["status"] in {"covered", "failed"}
    )
    negative_coverage_pct = (
        round((negative_slots_tested / negative_slots_total) * 100.0, 2)
        if negative_slots_total
        else 0.0
    )

    all_project_assertions = [
        a for sc in norm_scenarios for a in (sc.get("assertions") or []) if isinstance(a, dict)
    ]
    invariant_assertions_total = len(all_project_assertions)
    invariant_assertions_passed = sum(
        1 for a in all_project_assertions if str(a.get("status") or "").lower() == "passed"
    )
    invariant_pass_rate_pct = (
        round((invariant_assertions_passed / invariant_assertions_total) * 100.0, 2)
        if invariant_assertions_total
        else 0.0
    )

    return {
        "project_id": project_id,
        "project_name": project_name,
        "metrics": {
            "total_endpoints": total_endpoints,
            "total_capabilities": total_capabilities,
            "live_capabilities": live_capabilities,
            "target_capabilities": target_capabilities,
            "capability_coverage_pct": capability_coverage_pct,
            "stateful_capabilities": stateful_capabilities,
            "state_transition_covered": state_transition_covered,
            "state_coverage_pct": state_coverage_pct,
            "role_matrix_cells_total": role_matrix_cells_total,
            "role_matrix_cells_covered": role_matrix_cells_covered,
            "role_matrix_coverage_pct": role_matrix_coverage_pct,
            "negative_slots_total": negative_slots_total,
            "negative_slots_tested": negative_slots_tested,
            "negative_coverage_pct": negative_coverage_pct,
            "invariant_assertions_total": invariant_assertions_total,
            "invariant_assertions_passed": invariant_assertions_passed,
            "invariant_pass_rate_pct": invariant_pass_rate_pct,
        },
        "modules": modules_out,
        "capabilities": capabilities_out,
    }
