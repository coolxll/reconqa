"""Lossless-for-testing HAR 1.2 archive slimmer and first-party JS deduplicator."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.utils.io import ensure_dir

_BINARY_MIME_PREFIXES = ("image/", "font/", "audio/", "video/")
_BINARY_MIME_TOKENS = ("octet-stream", "pdf", "woff")
_BINARY_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".mp4",
    ".mp3",
    ".pdf",
    ".map",
)
_CSS_EXTS = (".css",)
_JS_EXTS = (".js", ".mjs")

_VENDOR_JS_TOKENS = (
    "elementui",
    "element-ui",
    "element-plus",
    "chunk-libs",
    "chunk-vendors",
    "echarts",
    "lodash",
    "tinymce",
    "moment",
    "xlsx",
    "quill",
    "pdf",
    "monaco",
    "jquery",
    "antd",
    "ant-design",
    "polyfill",
    "core-js",
    "codemirror",
    "ckeditor",
    "wangeditor",
    "vendor",
)

_MAX_NON_JSON_TEXT_BYTES = 2 * 1024 * 1024


def _is_vendor_js_name(url_or_path: str) -> bool:
    name = Path(urlparse(url_or_path).path or url_or_path).name.lower()
    if not name:
        return False
    return any(tok in name for tok in _VENDOR_JS_TOKENS)


def _decode_content_bytes(content: dict[str, Any]) -> tuple[bytes, str]:
    """Return (raw_bytes, utf8_text) for a HAR response content object."""
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return b"", ""
    encoding = str(content.get("encoding") or "").strip().lower()
    if encoding == "base64":
        try:
            raw = base64.b64decode(text, validate=False)
            return raw, raw.decode("utf-8", errors="replace")
        except Exception:
            raw = text.encode("utf-8", errors="replace")
            return raw, text
    raw = text.encode("utf-8", errors="replace")
    return raw, text


def _preserve_content_size(content: dict[str, Any], raw_bytes: bytes) -> None:
    cur_size = content.get("size")
    if not isinstance(cur_size, (int, float)) or cur_size <= 0:
        content["size"] = len(raw_bytes)


def _is_binary_entry(mime_low: str, path_low: str) -> bool:
    if any(mime_low.startswith(pref) for pref in _BINARY_MIME_PREFIXES):
        return True
    if any(tok in mime_low for tok in _BINARY_MIME_TOKENS):
        return True
    return any(path_low.endswith(ext) for ext in _BINARY_EXTS)


def _is_css_entry(mime_low: str, path_low: str) -> bool:
    return "css" in mime_low or any(path_low.endswith(ext) for ext in _CSS_EXTS)


def _is_js_entry(mime_low: str, path_low: str) -> bool:
    if "javascript" in mime_low or "ecmascript" in mime_low:
        return True
    return any(path_low.endswith(ext) for ext in _JS_EXTS)


def slim_har_file(
    har_path: Path,
    js_bundles_dir: Path | None = None,
    *,
    strip_binary: bool = True,
    strip_css: bool = True,
    extract_js: bool = True,
) -> dict[str, Any]:
    """Slim a HAR 1.2 file in-place while preserving 100% of API JSON, timings, headers, and sizes.

    Strips binary media/font/CSS payloads and deduplicates first-party JS bundles into
    ``js_bundles_dir`` by SHA-256 prefix so static endpoint extraction & security scanning
    continue working with minimal disk footprint.
    """
    target = Path(har_path)
    if not target.is_file():
        return {"slimmed": False, "reason": "file_not_found"}

    original_bytes = target.stat().st_size
    try:
        doc = json.loads(target.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {"slimmed": False, "original_bytes": original_bytes, "reason": "invalid_json"}

    if not isinstance(doc, dict) or not isinstance(doc.get("log"), dict):
        return {"slimmed": False, "original_bytes": original_bytes, "reason": "not_har"}

    entries = doc["log"].get("entries")
    if not isinstance(entries, list):
        return {"slimmed": False, "original_bytes": original_bytes, "reason": "no_entries"}

    stripped_binary_count = 0
    stripped_js_count = 0
    extracted_js_files = 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        if not isinstance(resp, dict):
            continue
        content = resp.get("content")
        if not isinstance(content, dict):
            continue

        raw_text = content.get("text")
        if not isinstance(raw_text, str) or not raw_text:
            continue

        url = str(req.get("url") or "") if isinstance(req, dict) else ""
        url_path = urlparse(url).path or ""
        path_low = url_path.lower()
        mime_low = str(content.get("mimeType") or "").split(";", 1)[0].strip().lower()

        is_bin = strip_binary and _is_binary_entry(mime_low, path_low)
        is_css = strip_css and _is_css_entry(mime_low, path_low)
        is_js = extract_js and _is_js_entry(mime_low, path_low)

        if is_bin or is_css:
            raw_bytes, _ = _decode_content_bytes(content)
            _preserve_content_size(content, raw_bytes)
            content.pop("text", None)
            content.pop("encoding", None)
            content["_reconqa_stripped"] = "binary_or_static"
            stripped_binary_count += 1
            continue

        if is_js:
            raw_bytes, decoded_text = _decode_content_bytes(content)
            _preserve_content_size(content, raw_bytes)
            if _is_vendor_js_name(url_path):
                content.pop("text", None)
                content.pop("encoding", None)
                content["_reconqa_stripped"] = "vendor_js"
                stripped_js_count += 1
                continue

            if js_bundles_dir is not None and decoded_text.strip():
                ensure_dir(js_bundles_dir)
                digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
                raw_stem = Path(url_path).stem or "bundle"
                safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_stem).strip("._-") or "bundle"
                dest_js = js_bundles_dir / f"{digest}_{safe_stem[:60]}.js"
                if not dest_js.exists():
                    dest_js.write_text(decoded_text, encoding="utf-8")
                    extracted_js_files += 1

            content.pop("text", None)
            content.pop("encoding", None)
            content["_reconqa_stripped"] = "js_extracted"
            stripped_js_count += 1
            continue

        # Keep API JSON / XML / HTML / plain text intact unless >2MB non-JSON blob
        is_json_like = "json" in mime_low or "graphql" in mime_low
        if not is_json_like:
            raw_bytes, _ = _decode_content_bytes(content)
            if len(raw_bytes) > _MAX_NON_JSON_TEXT_BYTES:
                _preserve_content_size(content, raw_bytes)
                content.pop("text", None)
                content.pop("encoding", None)
                content["_reconqa_stripped"] = "binary_or_static"
                stripped_binary_count += 1

    compact_payload = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=".slim_", suffix=".har", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(compact_payload)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    slimmed_bytes = target.stat().st_size
    saved_pct = (
        round(((original_bytes - slimmed_bytes) / original_bytes) * 100.0, 2)
        if original_bytes > 0
        else 0.0
    )
    return {
        "slimmed": True,
        "original_bytes": original_bytes,
        "slimmed_bytes": slimmed_bytes,
        "saved_pct": max(0.0, saved_pct),
        "stripped_binary_count": stripped_binary_count,
        "stripped_js_count": stripped_js_count,
        "extracted_js_files": extracted_js_files,
    }
