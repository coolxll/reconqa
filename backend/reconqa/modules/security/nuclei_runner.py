"""Wrappers for external Nuclei configuration scans and OWASP ZAP alert retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from reconqa.config import settings
from reconqa.utils.io import ensure_dir, read_jsonl
from reconqa.utils.proc import run_cmd_sync

ZAP_RISK_MAP = {
    "informational": "info",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
}


def run_nuclei_scan(
    target_url: str,
    out_dir: Path,
    *,
    severity: str = "low,medium,high,critical",
    timeout: int = 300,
) -> dict[str, Any]:
    """Run local Nuclei binary against `target_url` and normalize JSONL findings.

    Handles missing `nuclei` binary gracefully without raising.
    """
    ensure_dir(out_dir)
    jsonl_path = out_dir / "nuclei.jsonl"
    nuclei_bin = settings.resolve_bin("nuclei")

    if not nuclei_bin:
        jsonl_path.write_text("", encoding="utf-8")
        return {
            "status": "skipped",
            "reason": "nuclei binary not found in PATH or RECONQA_BIN_DIR",
            "target_url": target_url,
            "jsonl_path": str(jsonl_path),
            "raw_count": 0,
            "findings": [],
        }

    cmd = [
        nuclei_bin,
        "-u",
        target_url,
        "-jsonl",
        "-severity",
        severity,
        "-silent",
        "-o",
        str(jsonl_path),
    ]
    res = run_cmd_sync(cmd, timeout=timeout)

    # Fallback if nuclei printed JSONL to stdout instead of writing -o
    if (not jsonl_path.exists() or jsonl_path.stat().st_size == 0) and res.stdout.strip():
        jsonl_path.write_text(res.stdout, encoding="utf-8")
    elif not jsonl_path.exists():
        jsonl_path.write_text("", encoding="utf-8")

    raw_records = read_jsonl(jsonl_path)
    findings: list[dict[str, Any]] = []
    for rec in raw_records:
        info = rec.get("info") if isinstance(rec.get("info"), dict) else {}
        sev = str(info.get("severity") or rec.get("severity") or "info").lower()
        template_id = str(rec.get("template-id") or rec.get("templateID") or "unknown")
        name = str(info.get("name") or template_id)
        matched = str(rec.get("matched-at") or rec.get("host") or target_url)
        findings.append(
            {
                "category": "nuclei",
                "severity": sev,
                "endpoint": matched,
                "title": f"[{template_id}] {name}",
                "evidence": {
                    "template_id": template_id,
                    "matcher_name": rec.get("matcher-name"),
                    "type": rec.get("type"),
                    "tags": info.get("tags", []),
                },
            }
        )

    status = "timed_out" if res.timed_out else ("completed" if res.returncode == 0 else "error")
    return {
        "status": status,
        "returncode": res.returncode,
        "timed_out": res.timed_out,
        "target_url": target_url,
        "jsonl_path": str(jsonl_path),
        "raw_count": len(raw_records),
        "findings": findings,
        "stderr": res.stderr[:1000] if res.stderr else "",
    }


def fetch_zap_alerts(
    target_url: str,
    zap_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch passive/active alerts for `target_url` from an OWASP ZAP API endpoint."""
    resolved_url = (zap_url or settings.zap_api_url or "").strip()
    resolved_key = api_key if api_key is not None else settings.zap_api_key

    if not resolved_url:
        return {
            "status": "skipped",
            "reason": "ZAP API URL not configured (set zap_url or RECONQA_ZAP_API_URL)",
            "target_url": target_url,
            "findings": [],
        }

    endpoint = f"{resolved_url.rstrip('/')}/JSON/core/view/alerts/"
    params: dict[str, str] = {"baseurl": target_url}
    if resolved_key:
        params["apikey"] = resolved_key

    try:
        resp = httpx.get(endpoint, params=params, timeout=settings.http_timeout)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, Exception) as exc:
        return {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "target_url": target_url,
            "findings": [],
        }

    alerts = payload.get("alerts") if isinstance(payload, dict) else []
    if not isinstance(alerts, list):
        alerts = []

    findings: list[dict[str, Any]] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        raw_risk = str(alert.get("risk") or "info").strip().lower()
        sev = ZAP_RISK_MAP.get(raw_risk, "info")
        alert_url = str(alert.get("url") or target_url)
        name = str(alert.get("alert") or alert.get("name") or "ZAP Alert")
        findings.append(
            {
                "category": "zap",
                "severity": sev,
                "endpoint": alert_url,
                "title": name,
                "evidence": {
                    "plugin_id": alert.get("pluginId"),
                    "cweid": alert.get("cweid"),
                    "confidence": alert.get("confidence"),
                    "param": alert.get("param"),
                },
            }
        )

    return {
        "status": "completed",
        "target_url": target_url,
        "zap_url": resolved_url,
        "raw_count": len(alerts),
        "findings": findings,
    }
