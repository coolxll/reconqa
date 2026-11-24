"""First-class HAR 1.2 importer, normalizer, and embedded JS bundle extractor."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.utils.io import ensure_dir, sha256_file

TIMING_KEYS = ("blocked", "dns", "connect", "send", "wait", "receive", "ssl")

TEXT_MIME_TOKENS = (
    "json",
    "javascript",
    "ecmascript",
    "text/",
    "xml",
    "html",
    "css",
    "x-www-form-urlencoded",
    "graphql",
)


def _sanitize_scenario_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._-")
    return cleaned or "scenario"


def _normalize_domain_filter(target_domain: str | None) -> str | None:
    if not target_domain:
        return None
    dom = target_domain.strip().lower()
    if "://" in dom:
        dom = urlparse(dom).hostname or dom
    return dom.lstrip("*.") or None


def _matches_domain(url: str, domain_filter: str | None) -> bool:
    if not domain_filter:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return host == domain_filter or host.endswith(f".{domain_filter}")


def _strip_pseudo_headers(headers: Any) -> list[dict[str, str]]:
    if not isinstance(headers, list):
        return []
    cleaned: list[dict[str, str]] = []
    for h in headers:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name", ""))
        if not name or name.startswith(":"):
            continue
        cleaned.append({"name": name, "value": str(h.get("value", ""))})
    return cleaned


def _sanitize_timings(timings: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    src = timings if isinstance(timings, dict) else {}
    for key in TIMING_KEYS:
        val = src.get(key, 0)
        if isinstance(val, (int, float)) and val >= 0:
            out[key] = float(val)
        else:
            out[key] = 0.0
    for key, val in src.items():
        if key in out:
            continue
        if isinstance(val, (int, float)):
            out[key] = float(val) if val >= 0 else 0.0
        else:
            out[key] = val
    return out


def _is_text_like_mime(mime_type: str) -> bool:
    low = (mime_type or "").lower()
    if not low:
        return True
    return any(tok in low for tok in TEXT_MIME_TOKENS)


def _decode_base64_content(content: dict[str, Any]) -> bool:
    """Decode base64-encoded UTF-8 text response content in-place. Returns True if decoded."""
    if not isinstance(content, dict):
        return False
    encoding = str(content.get("encoding", "")).lower().strip()
    text = content.get("text")
    if encoding != "base64" or not isinstance(text, str) or not text:
        return False
    mime_type = str(content.get("mimeType", ""))
    if not _is_text_like_mime(mime_type):
        return False
    try:
        raw_bytes = base64.b64decode(text, validate=False)
        decoded = raw_bytes.decode("utf-8")
    except Exception:
        return False
    if "\x00" in decoded:
        return False
    content["text"] = decoded
    content.pop("encoding", None)
    if not isinstance(content.get("size"), (int, float)) or content["size"] < 0:
        content["size"] = len(raw_bytes)
    return True


def _is_javascript_entry(url: str, mime_type: str) -> bool:
    low_mime = (mime_type or "").lower()
    if "javascript" in low_mime or "ecmascript" in low_mime:
        return True
    path = (urlparse(url).path or "").lower()
    return path.endswith((".js", ".mjs", ".cjs"))


def _safe_js_filename(url: str, content_hash: str) -> str:
    path = urlparse(url).path or ""
    base = Path(path).name if path else ""
    if not base or not base.lower().endswith((".js", ".mjs", ".cjs")):
        base = f"bundle_{content_hash[:8]}.js"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if not safe.lower().endswith((".js", ".mjs", ".cjs")):
        safe = f"{safe}.js"
    return safe or f"bundle_{content_hash[:8]}.js"


def _discover_source_files(sources: list[Path]) -> list[Path]:
    files: list[Path] = []
    for raw_src in sources:
        src = Path(raw_src)
        if not src.exists():
            raise FileNotFoundError(f"HAR source not found: {src}")
        if src.is_file():
            files.append(src)
        elif src.is_dir():
            direct = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() == ".har")
            if direct:
                files.extend(direct)
            else:
                files.extend(sorted(p for p in src.rglob("*.har") if p.is_file()))
    return files


def import_hars(
    sources: list[Path],
    out_dir: Path,
    *,
    scenario_name: str | None = None,
    target_domain: str | None = None,
    extract_js: bool = True,
) -> dict[str, Any]:
    """Import, normalize, filter, and extract embedded JS bundles from existing HAR 1.2 files."""
    ensure_dir(out_dir)
    domain_filter = _normalize_domain_filter(target_domain)
    source_files = _discover_source_files(sources)

    used_scenarios: set[str] = set()
    imported_records: list[dict[str, Any]] = []
    total_entries = 0
    filtered_out_entries = 0
    base64_decoded_count = 0
    total_js_extracted = 0
    js_bundles_root = out_dir / "js_bundles"

    for idx, src_path in enumerate(source_files, start=1):
        raw_har = json.loads(src_path.read_text(encoding="utf-8", errors="replace"))
        log_obj = raw_har.get("log") if isinstance(raw_har, dict) else None
        if not isinstance(log_obj, dict):
            log_obj = {
                "version": "1.2",
                "creator": {"name": "ReconQA", "version": "0.1.0"},
                "entries": [],
            }
        raw_entries = log_obj.get("entries") or []

        if scenario_name:
            base_scenario = (
                _sanitize_scenario_name(scenario_name)
                if len(source_files) == 1
                else _sanitize_scenario_name(f"{scenario_name}_{src_path.stem}")
            )
        else:
            base_scenario = _sanitize_scenario_name(src_path.stem)

        scenario = base_scenario
        if scenario in used_scenarios:
            scenario = f"{base_scenario}_{idx:02d}"
        used_scenarios.add(scenario)

        normalized_entries: list[dict[str, Any]] = []
        extracted_js_files: list[str] = []
        seen_js_hashes: set[str] = set()
        used_js_names: set[str] = set()

        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            req = entry.get("request")
            if not isinstance(req, dict):
                continue
            url = str(req.get("url", ""))
            if not _matches_domain(url, domain_filter):
                filtered_out_entries += 1
                continue

            norm_entry = copy.deepcopy(entry)

            # Strip HTTP/2 pseudo-headers from request & response
            norm_req = norm_entry.setdefault("request", {})
            norm_req["headers"] = _strip_pseudo_headers(norm_req.get("headers"))

            norm_resp = norm_entry.setdefault("response", {})
            norm_resp["headers"] = _strip_pseudo_headers(norm_resp.get("headers"))

            # Sanitize negative timings & entry time
            norm_timings = _sanitize_timings(norm_entry.get("timings"))
            norm_entry["timings"] = norm_timings
            raw_time = norm_entry.get("time")
            if not isinstance(raw_time, (int, float)) or raw_time < 0:
                norm_entry["time"] = sum(
                    norm_timings[k]
                    for k in ("blocked", "dns", "connect", "send", "wait", "receive")
                )

            # Decode base64 text responses
            content = norm_resp.setdefault("content", {})
            if isinstance(content, dict) and _decode_base64_content(content):
                base64_decoded_count += 1

            # Extract embedded JS bundle
            if extract_js and isinstance(content, dict):
                mime_type = str(content.get("mimeType", ""))
                body_text = content.get("text")
                if (
                    _is_javascript_entry(url, mime_type)
                    and isinstance(body_text, str)
                    and body_text.strip()
                ):
                    digest = hashlib.sha256(body_text.encode("utf-8", errors="ignore")).hexdigest()
                    if digest not in seen_js_hashes:
                        seen_js_hashes.add(digest)
                        js_dir = ensure_dir(js_bundles_root / scenario)
                        fname = _safe_js_filename(url, digest)
                        if fname in used_js_names:
                            stem = Path(fname).stem
                            suffix = Path(fname).suffix or ".js"
                            fname = f"{stem}_{digest[:8]}{suffix}"
                        used_js_names.add(fname)
                        js_file = js_dir / fname
                        js_file.write_text(body_text, encoding="utf-8")
                        extracted_js_files.append(str(js_file))
                        total_js_extracted += 1

            normalized_entries.append(norm_entry)

        out_har_obj = {
            "log": {
                "version": str(log_obj.get("version") or "1.2"),
                "creator": log_obj.get("creator") or {"name": "ReconQA", "version": "0.1.0"},
                "pages": log_obj.get("pages") or [],
                "entries": normalized_entries,
            }
        }
        dest_har = out_dir / f"{scenario}.har"
        # Atomic temp-file + os.replace breaks any existing hardlink inode cleanly
        fd, tmp_name = tempfile.mkstemp(prefix=".har_import_", suffix=".har", dir=str(out_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(out_har_obj, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, dest_har)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        total_entries += len(normalized_entries)
        imported_records.append(
            {
                "scenario": scenario,
                "source": str(src_path),
                "output_path": str(dest_har),
                "sha256": sha256_file(dest_har),
                "entries": len(normalized_entries),
                "extracted_js": extracted_js_files,
            }
        )

    return {
        "out_dir": str(out_dir),
        "imported_count": len(imported_records),
        "total_entries": total_entries,
        "filtered_out_entries": filtered_out_entries,
        "base64_decoded_count": base64_decoded_count,
        "extracted_js_count": total_js_extracted,
        "js_bundles_dir": str(js_bundles_root) if total_js_extracted else None,
        "hars": imported_records,
    }
