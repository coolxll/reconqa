"""Asset & endpoint discovery orchestrator wrapping ProjectDiscovery CLIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.config import settings
from reconqa.modules.discovery.spa_parser import extract_spa_routes
from reconqa.utils.io import ensure_dir, write_json, write_jsonl
from reconqa.utils.proc import run_cmd_sync


def _normalize_target(target: str) -> tuple[str, str]:
    """Return (domain, target_url) from a domain or URL target."""
    raw = target.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        domain = (parsed.hostname or raw).lower()
        target_url = raw
    else:
        parsed = urlparse(f"https://{raw}")
        domain = (parsed.hostname or raw.split("/")[0]).lower()
        target_url = f"https://{raw}"
    return domain, target_url


def _parse_jsonl_output(stdout: str, fallback_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
            else:
                records.append({fallback_key: obj})
        except json.JSONDecodeError:
            records.append({fallback_key: line})
    return records


def run_discovery(
    target: str,
    out_dir: Path,
    *,
    mode: str = "passive",
    depth: int = 3,
    crawl_time: str = "5m",
    concurrency: int = 10,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run passive/active discovery using ProjectDiscovery CLIs + SPA route extraction.

    Missing external binaries are logged as warnings and skipped without crashing.
    """
    ensure_dir(out_dir)
    domain, target_url = _normalize_target(target)
    eff_timeout = timeout if timeout is not None else settings.default_cmd_timeout
    normalized_mode = (mode or "passive").lower().strip()

    warnings: list[str] = []
    tools_used: dict[str, str] = {}
    artifacts: dict[str, str] = {}
    counts: dict[str, int] = {
        "subdomains": 0,
        "historical_urls": 0,
        "tls_sans": 0,
        "katana_urls": 0,
        "live_hosts": 0,
    }

    discovered_hosts: set[str] = {domain}
    discovered_urls: set[str] = {target_url}

    # --- Passive Step 1: subfinder ---
    subfinder_bin = settings.resolve_bin("subfinder")
    if subfinder_bin:
        tools_used["subfinder"] = subfinder_bin
        res = run_cmd_sync(
            [subfinder_bin, "-d", domain, "-silent", "-json"],
            timeout=eff_timeout,
        )
        if res.timed_out:
            warnings.append(f"subfinder timed out after {eff_timeout}s")
        elif res.returncode != 0 and not res.stdout.strip():
            warnings.append(
                f"subfinder exited with code {res.returncode}: {res.stderr.strip()[:200]}"
            )
        records = _parse_jsonl_output(res.stdout, "host")
        out_path = write_jsonl(out_dir / "subdomains.jsonl", records)
        artifacts["subdomains"] = str(out_path)
        counts["subdomains"] = len(records)
        for rec in records:
            host = rec.get("host")
            if isinstance(host, str) and host.strip():
                discovered_hosts.add(host.strip().lower())
    else:
        warnings.append("subfinder binary not found; skipped subdomain discovery")

    # --- Passive Step 2: urlfinder ---
    urlfinder_bin = settings.resolve_bin("urlfinder")
    if urlfinder_bin:
        tools_used["urlfinder"] = urlfinder_bin
        res = run_cmd_sync(
            [urlfinder_bin, "-d", domain, "-silent", "-json"],
            timeout=eff_timeout,
        )
        if res.timed_out:
            warnings.append(f"urlfinder timed out after {eff_timeout}s")
        elif res.returncode != 0 and not res.stdout.strip():
            warnings.append(
                f"urlfinder exited with code {res.returncode}: {res.stderr.strip()[:200]}"
            )
        records = _parse_jsonl_output(res.stdout, "url")
        out_path = write_jsonl(out_dir / "historical_urls.jsonl", records)
        artifacts["historical_urls"] = str(out_path)
        counts["historical_urls"] = len(records)
        for rec in records:
            url_val = rec.get("url")
            if isinstance(url_val, str) and url_val.startswith(("http://", "https://")):
                discovered_urls.add(url_val.strip())
    else:
        warnings.append("urlfinder binary not found; skipped historical URL discovery")

    # --- Passive Step 3: tlsx ---
    tlsx_bin = settings.resolve_bin("tlsx")
    if tlsx_bin:
        tools_used["tlsx"] = tlsx_bin
        tlsx_input = "\n".join(sorted(discovered_hosts)[:200]) + "\n"
        res = run_cmd_sync(
            [tlsx_bin, "-san", "-cn", "-silent", "-json"],
            timeout=eff_timeout,
            input_text=tlsx_input,
        )
        if res.timed_out:
            warnings.append(f"tlsx timed out after {eff_timeout}s")
        elif res.returncode != 0 and not res.stdout.strip():
            warnings.append(f"tlsx exited with code {res.returncode}: {res.stderr.strip()[:200]}")
        records = _parse_jsonl_output(res.stdout, "host")
        out_path = write_jsonl(out_dir / "tls_sans.jsonl", records)
        artifacts["tls_sans"] = str(out_path)
        counts["tls_sans"] = len(records)
        for rec in records:
            sans = rec.get("subject_an") or rec.get("san") or []
            if isinstance(sans, list):
                for san in sans:
                    if isinstance(san, str) and not san.startswith("*."):
                        discovered_hosts.add(san.strip().lower())
    else:
        warnings.append("tlsx binary not found; skipped TLS SAN discovery")

    # --- Active Steps (katana + httpx) ---
    if normalized_mode == "active":
        katana_bin = settings.resolve_bin("katana")
        if katana_bin:
            tools_used["katana"] = katana_bin
            res = run_cmd_sync(
                [
                    katana_bin,
                    "-u",
                    target_url,
                    "-jc",
                    "-kf",
                    "all",
                    "-d",
                    str(depth),
                    "-ct",
                    str(crawl_time),
                    "-c",
                    str(concurrency),
                    "-jsonl",
                    "-silent",
                ],
                timeout=eff_timeout,
            )
            if res.timed_out:
                warnings.append(f"katana timed out after {eff_timeout}s")
            elif res.returncode != 0 and not res.stdout.strip():
                warnings.append(
                    f"katana exited with code {res.returncode}: {res.stderr.strip()[:200]}"
                )
            records = _parse_jsonl_output(res.stdout, "endpoint")
            out_path = write_jsonl(out_dir / "katana_urls.jsonl", records)
            artifacts["katana_urls"] = str(out_path)
            counts["katana_urls"] = len(records)
            for rec in records:
                req = rec.get("request")
                ep = req.get("endpoint") if isinstance(req, dict) else rec.get("endpoint")
                if isinstance(ep, str) and ep.startswith(("http://", "https://")):
                    discovered_urls.add(ep.strip())
        else:
            warnings.append("katana binary not found; skipped active JS/link crawl")

        httpx_bin = settings.resolve_bin("httpx")
        if httpx_bin:
            tools_used["httpx"] = httpx_bin
            probe_targets = sorted(discovered_hosts | {target_url})[:300]
            res = run_cmd_sync(
                [
                    httpx_bin,
                    "-silent",
                    "-json",
                    "-td",
                    "-cdn",
                    "-status-code",
                    "-title",
                    "-content-length",
                    "-response-time",
                    "-threads",
                    str(concurrency),
                ],
                timeout=eff_timeout,
                input_text="\n".join(probe_targets) + "\n",
            )
            if res.timed_out:
                warnings.append(f"httpx timed out after {eff_timeout}s")
            elif res.returncode != 0 and not res.stdout.strip():
                warnings.append(
                    f"httpx exited with code {res.returncode}: {res.stderr.strip()[:200]}"
                )
            records = _parse_jsonl_output(res.stdout, "url")
            out_path = write_jsonl(out_dir / "live_hosts.jsonl", records)
            artifacts["live_hosts"] = str(out_path)
            counts["live_hosts"] = len(records)
            for rec in records:
                url_val = rec.get("url")
                if isinstance(url_val, str) and url_val.startswith(("http://", "https://")):
                    discovered_urls.add(url_val.strip())
        else:
            warnings.append("httpx binary not found; skipped live host probing")

    # --- SPA & Micro-frontend baseline ---
    spa_result = extract_spa_routes(out_dir, urls=sorted(discovered_urls))
    artifacts["spa_routes"] = spa_result["output_file"]
    counts["spa_routes"] = spa_result["total_routes"]
    counts["micro_frontends"] = len(spa_result["micro_frontends"])

    summary: dict[str, Any] = {
        "target": target,
        "domain": domain,
        "target_url": target_url,
        "mode": normalized_mode,
        "out_dir": str(out_dir),
        "tools_used": tools_used,
        "warnings": warnings,
        "counts": counts,
        "artifacts": artifacts,
        "spa_routes": spa_result,
    }
    write_json(out_dir / "discovery_summary.json", summary)
    return summary
