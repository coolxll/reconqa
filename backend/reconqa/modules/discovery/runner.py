"""Asset & endpoint discovery orchestrator wrapping ProjectDiscovery CLIs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.config import settings
from reconqa.modules.discovery.spa_parser import extract_spa_routes
from reconqa.utils.io import ensure_dir, write_json, write_jsonl
from reconqa.utils.proc import run_cmd_sync

_PROXY_CRED_RE = re.compile(r"((?:https?|socks5h?)://)([^/\s@]+)@", re.IGNORECASE)


def _redact_proxy_creds(text: str) -> str:
    return _PROXY_CRED_RE.sub(r"\1***@", text)


def _resolve_pd_proxies() -> tuple[str | None, str | None, str | None]:
    """Return (http_proxy, http_or_socks_proxy, socks5_proxy) from env/settings without logging secrets."""
    raw_candidates = [
        os.environ.get("RECONQA_PROXY_URL"),
        settings.k6_proxy_url,
        os.environ.get("HTTPS_PROXY"),
        os.environ.get("https_proxy"),
        os.environ.get("HTTP_PROXY"),
        os.environ.get("http_proxy"),
        os.environ.get("ALL_PROXY"),
        os.environ.get("all_proxy"),
    ]
    cleaned: list[str] = []
    for val in raw_candidates:
        if isinstance(val, str) and val.strip():
            cleaned.append(val.strip())

    http_proxy: str | None = None
    any_proxy: str | None = None
    socks5_proxy: str | None = None
    for p in cleaned:
        low = p.lower()
        if http_proxy is None and low.startswith(("http://", "https://")):
            http_proxy = p
        if socks5_proxy is None and low.startswith(("socks5://", "socks5h://")):
            socks5_proxy = p
        if any_proxy is None and low.startswith(("http://", "https://", "socks5://", "socks5h://")):
            any_proxy = p
    return http_proxy, any_proxy, socks5_proxy


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
    http_proxy, any_proxy, socks5_proxy = _resolve_pd_proxies()
    short_timeout = eff_timeout <= 60

    # --- Passive Step 1: subfinder ---
    subfinder_bin = settings.resolve_bin("subfinder")
    if subfinder_bin:
        tools_used["subfinder"] = subfinder_bin
        sf_cmd = [subfinder_bin, "-d", domain, "-silent", "-json"]
        if short_timeout:
            sf_cmd.extend(["-timeout", "5", "-max-time", "1"])
        if http_proxy:
            sf_cmd.extend(["-proxy", http_proxy])
        res = run_cmd_sync(sf_cmd, timeout=eff_timeout)
        if res.timed_out:
            warnings.append(f"subfinder timed out after {eff_timeout}s")
        elif res.returncode != 0 and not res.stdout.strip():
            warnings.append(
                f"subfinder exited with code {res.returncode}: "
                f"{_redact_proxy_creds(res.stderr.strip()[:200])}"
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
        uf_cmd = [urlfinder_bin, "-d", domain, "-silent", "-json"]
        if short_timeout:
            uf_cmd.extend(["-timeout", "5"])
        if http_proxy:
            uf_cmd.extend(["-proxy", http_proxy])
        res = run_cmd_sync(uf_cmd, timeout=eff_timeout)
        if res.timed_out:
            warnings.append(f"urlfinder timed out after {eff_timeout}s")
        elif res.returncode != 0 and not res.stdout.strip():
            warnings.append(
                f"urlfinder exited with code {res.returncode}: "
                f"{_redact_proxy_creds(res.stderr.strip()[:200])}"
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
        tlsx_cmd = [tlsx_bin, "-san", "-cn", "-silent", "-json"]
        if short_timeout:
            tlsx_cmd.extend(["-timeout", "5"])
        if socks5_proxy:
            tlsx_cmd.extend(["-proxy", socks5_proxy])
        res = run_cmd_sync(
            tlsx_cmd,
            timeout=eff_timeout,
            input_text=tlsx_input,
        )
        if res.timed_out:
            warnings.append(f"tlsx timed out after {eff_timeout}s")
        elif res.returncode != 0 and not res.stdout.strip():
            warnings.append(
                f"tlsx exited with code {res.returncode}: "
                f"{_redact_proxy_creds(res.stderr.strip()[:200])}"
            )
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
            kat_cmd = [
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
            ]
            if any_proxy:
                kat_cmd.extend(["-proxy", any_proxy])
            res = run_cmd_sync(kat_cmd, timeout=eff_timeout)
            if res.timed_out:
                warnings.append(f"katana timed out after {eff_timeout}s")
            elif res.returncode != 0 and not res.stdout.strip():
                warnings.append(
                    f"katana exited with code {res.returncode}: "
                    f"{_redact_proxy_creds(res.stderr.strip()[:200])}"
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
            hx_cmd = [
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
            ]
            if short_timeout:
                hx_cmd.extend(["-timeout", "5"])
            if any_proxy:
                hx_cmd.extend(["-http-proxy", any_proxy])
            res = run_cmd_sync(
                hx_cmd,
                timeout=eff_timeout,
                input_text="\n".join(probe_targets) + "\n",
            )
            if res.timed_out:
                warnings.append(f"httpx timed out after {eff_timeout}s")
            elif res.returncode != 0 and not res.stdout.strip():
                warnings.append(
                    f"httpx exited with code {res.returncode}: "
                    f"{_redact_proxy_creds(res.stderr.strip()[:200])}"
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
