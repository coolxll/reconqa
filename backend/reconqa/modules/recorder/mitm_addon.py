"""Standalone mitmproxy addon for scenario-aware HAR 1.2 recording & JS bundle extraction."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from reconqa.config import settings
from reconqa.utils.io import ensure_dir, write_json

try:
    from mitmproxy import ctx, http  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    ctx = None
    http = None

CONTROL_HOST = "reconqa.local"

STATIC_NOISE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".avif",
    ".bmp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".css",
    ".scss",
    ".mp4",
    ".mp3",
    ".webm",
    ".wav",
    ".pdf",
    ".map",
)

STATIC_NOISE_MIME_PREFIXES = (
    "image/",
    "font/",
    "audio/",
    "video/",
    "text/css",
)


def _sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._-")
    return cleaned or "01_manual"


def _normalize_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    dom = domain.strip().lower()
    if "://" in dom:
        dom = urlparse(dom).hostname or dom
    return dom.lstrip("*.") or None


def _is_static_noise(url: str, mime_type: str) -> bool:
    low_mime = (mime_type or "").lower()
    if "javascript" in low_mime or "ecmascript" in low_mime:
        return False
    if any(low_mime.startswith(pfx) for pfx in STATIC_NOISE_MIME_PREFIXES):
        return True
    path = (urlparse(url).path or "").lower()
    return path.endswith(STATIC_NOISE_EXTENSIONS)


def _is_javascript(url: str, mime_type: str) -> bool:
    low_mime = (mime_type or "").lower()
    if "javascript" in low_mime or "ecmascript" in low_mime:
        return True
    path = (urlparse(url).path or "").lower()
    return path.endswith((".js", ".mjs", ".cjs"))


def _clean_headers(raw_headers: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if raw_headers is None or not hasattr(raw_headers, "items"):
        return out
    try:
        iterator = list(raw_headers.items(multi=True))
    except TypeError:
        iterator = list(raw_headers.items())
    for k, v in iterator:
        name = str(k)
        if not name or name.startswith(":"):
            continue
        out.append({"name": name, "value": str(v)})
    return out


class ReconQAHarAddon:
    """mitmproxy addon capturing scenario-partitioned HAR 1.2 files + JS bundles."""

    def __init__(
        self,
        out_dir: str | Path = "./har",
        scenario: str = "01_manual",
        target_domain: str | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.current_scenario = _sanitize_name(scenario)
        self.target_domain = _normalize_domain(target_domain)
        self._entries: dict[str, list[dict[str, Any]]] = {}
        self._seen_js_hashes: dict[str, set[str]] = {}

    def load(self, loader: Any) -> None:
        loader.add_option(
            name="reconqa_out",
            typespec=str,
            default=str(self.out_dir),
            help="Output directory for ReconQA scenario HAR files",
        )
        loader.add_option(
            name="reconqa_scenario",
            typespec=str,
            default=self.current_scenario,
            help="Initial scenario name for HAR recording",
        )
        loader.add_option(
            name="reconqa_domain",
            typespec=str,
            default=self.target_domain or "",
            help="Optional target domain filter (e.g. example.com)",
        )

    def configure(self, updates: set[str]) -> None:
        if ctx is None:
            return
        if "reconqa_out" in updates:
            self.out_dir = Path(ctx.options.reconqa_out)
        if "reconqa_scenario" in updates:
            self.current_scenario = _sanitize_name(ctx.options.reconqa_scenario)
        if "reconqa_domain" in updates:
            self.target_domain = _normalize_domain(ctx.options.reconqa_domain)

    def _make_json_response(self, payload: dict[str, Any], status: int = 200) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if http is not None:
            return http.Response.make(
                status,
                body,
                {"Content-Type": "application/json; charset=utf-8"},
            )
        return {"status": status, "body": payload}

    def request(self, flow: Any) -> None:
        raw_host = getattr(flow.request, "pretty_host", "") or getattr(flow.request, "host", "")
        host = str(raw_host).lower()
        if host != CONTROL_HOST:
            return
        path = getattr(flow.request, "path", "/") or "/"
        if path.startswith("/scenario/"):
            new_name = _sanitize_name(path[len("/scenario/") :].split("?", 1)[0])
            prev = self.current_scenario
            flushed = self.flush_scenario(prev)
            self.current_scenario = new_name
            flow.response = self._make_json_response(
                {
                    "status": "ok",
                    "previous_scenario": prev,
                    "current_scenario": self.current_scenario,
                    "flushed_entries": flushed,
                }
            )
        elif path.startswith("/flush"):
            flushed = self.flush_scenario(self.current_scenario)
            flow.response = self._make_json_response(
                {
                    "status": "ok",
                    "scenario": self.current_scenario,
                    "flushed_entries": flushed,
                }
            )
        else:
            flow.response = self._make_json_response(
                {
                    "status": "ok",
                    "scenario": self.current_scenario,
                    "buffered_entries": len(self._entries.get(self.current_scenario, [])),
                    "out_dir": str(self.out_dir),
                    "target_domain": self.target_domain,
                }
            )

    def response(self, flow: Any) -> None:
        raw_host = getattr(flow.request, "pretty_host", "") or getattr(flow.request, "host", "")
        host = str(raw_host).lower()
        if host == CONTROL_HOST:
            return
        if self.target_domain and not (
            host == self.target_domain or host.endswith(f".{self.target_domain}")
        ):
            return
        if flow.response is None:
            return

        url = str(getattr(flow.request, "pretty_url", "") or getattr(flow.request, "url", ""))
        mime = str(flow.response.headers.get("content-type", "")).split(";", 1)[0].strip()
        if _is_static_noise(url, mime):
            return

        entry = self.flow_to_har_entry(flow)
        self._entries.setdefault(self.current_scenario, []).append(entry)

        if _is_javascript(url, mime):
            content_text = entry["response"]["content"].get("text", "")
            if content_text and entry["response"]["content"].get("encoding") != "base64":
                self._save_js_bundle(self.current_scenario, url, content_text)

    def _save_js_bundle(self, scenario: str, url: str, text: str) -> None:
        digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        seen = self._seen_js_hashes.setdefault(scenario, set())
        if digest in seen:
            return
        seen.add(digest)
        js_dir = ensure_dir(self.out_dir / "js_bundles" / scenario)
        raw_name = Path(urlparse(url).path).name or f"bundle_{digest[:8]}.js"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._") or f"bundle_{digest[:8]}.js"
        if not safe.lower().endswith((".js", ".mjs", ".cjs")):
            safe = f"{safe}.js"
        dest = js_dir / safe
        if dest.exists():
            dest = js_dir / f"{dest.stem}_{digest[:8]}{dest.suffix}"
        dest.write_text(text, encoding="utf-8")

    def flow_to_har_entry(self, flow: Any) -> dict[str, Any]:
        req = flow.request
        resp = flow.response
        url = str(getattr(req, "pretty_url", "") or getattr(req, "url", ""))
        parsed = urlparse(url)

        ts_start = float(getattr(req, "timestamp_start", 0.0) or 0.0)
        ts_req_end = float(getattr(req, "timestamp_end", ts_start) or ts_start)
        ts_resp_start = float(getattr(resp, "timestamp_start", ts_req_end) or ts_req_end)
        ts_resp_end = float(getattr(resp, "timestamp_end", ts_resp_start) or ts_resp_start)

        started_dt = (
            datetime.fromtimestamp(ts_start, tz=UTC).isoformat().replace("+00:00", "Z")
            if ts_start > 0
            else datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        )
        send_ms = max(0.0, (ts_req_end - ts_start) * 1000.0)
        wait_ms = max(0.0, (ts_resp_start - ts_req_end) * 1000.0)
        receive_ms = max(0.0, (ts_resp_end - ts_resp_start) * 1000.0)
        total_ms = round(send_ms + wait_ms + receive_ms, 2)

        req_raw: bytes = getattr(req, "raw_content", None) or b""
        req_mime = str(req.headers.get("content-type", ""))
        post_data: dict[str, Any] | None = None
        if req_raw:
            post_data = {
                "mimeType": req_mime,
                "text": req_raw.decode("utf-8", errors="replace"),
            }

        resp_raw: bytes = getattr(resp, "content", None) or b""
        resp_mime = str(resp.headers.get("content-type", "")).split(";", 1)[0].strip()
        resp_content: dict[str, Any] = {"size": len(resp_raw), "mimeType": resp_mime}
        try:
            decoded = resp_raw.decode("utf-8")
            if "\x00" in decoded:
                raise UnicodeError("binary NUL byte")
            resp_content["text"] = decoded
        except UnicodeError:
            resp_content["text"] = base64.b64encode(resp_raw).decode("ascii")
            resp_content["encoding"] = "base64"

        req_obj: dict[str, Any] = {
            "method": str(getattr(req, "method", "GET")).upper(),
            "url": url,
            "httpVersion": str(getattr(req, "http_version", "HTTP/1.1")),
            "cookies": [],
            "headers": _clean_headers(req.headers),
            "queryString": [
                {"name": k, "value": v} for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            "headersSize": -1,
            "bodySize": len(req_raw),
        }
        if post_data is not None:
            req_obj["postData"] = post_data

        resp_obj: dict[str, Any] = {
            "status": int(getattr(resp, "status_code", 0) or 0),
            "statusText": str(getattr(resp, "reason", "") or ""),
            "httpVersion": str(getattr(resp, "http_version", "HTTP/1.1")),
            "cookies": [],
            "headers": _clean_headers(resp.headers),
            "content": resp_content,
            "redirectURL": str(resp.headers.get("location", "")),
            "headersSize": -1,
            "bodySize": len(resp_raw),
        }

        return {
            "startedDateTime": started_dt,
            "time": total_ms,
            "request": req_obj,
            "response": resp_obj,
            "cache": {},
            "timings": {
                "blocked": 0.0,
                "dns": 0.0,
                "connect": 0.0,
                "send": round(send_ms, 2),
                "wait": round(wait_ms, 2),
                "receive": round(receive_ms, 2),
                "ssl": 0.0,
            },
        }

    def flush_scenario(self, scenario: str | None = None) -> int:
        target = _sanitize_name(scenario or self.current_scenario)
        entries = self._entries.get(target, [])
        if not entries:
            return 0
        ensure_dir(self.out_dir)
        har_path = self.out_dir / f"{target}.har"
        existing_entries: list[dict[str, Any]] = []
        if har_path.is_file():
            try:
                prev = json.loads(har_path.read_text(encoding="utf-8"))
                existing_entries = list(prev.get("log", {}).get("entries", []))
            except Exception:
                existing_entries = []
        combined = existing_entries + entries
        har_doc = {
            "log": {
                "version": "1.2",
                "creator": {"name": "ReconQA mitmproxy addon", "version": "0.1.0"},
                "pages": [],
                "entries": combined,
            }
        }
        write_json(har_path, har_doc, indent=2)
        flushed = len(entries)
        self._entries[target] = []
        return flushed

    def done(self) -> None:
        for scenario in list(self._entries.keys()):
            self.flush_scenario(scenario)


def build_mitmdump_command(
    out_dir: Path,
    port: int = 8080,
    scenario: str = "01_manual",
    target_domain: str | None = None,
) -> list[str]:
    """Build a standalone mitmdump CLI invocation using this addon script."""
    mitmdump_bin = settings.resolve_bin("mitmdump") or "mitmdump"
    addon_script = str(Path(__file__).resolve())
    cmd = [
        mitmdump_bin,
        "-p",
        str(port),
        "-s",
        addon_script,
        "--set",
        f"reconqa_out={out_dir}",
        "--set",
        f"reconqa_scenario={_sanitize_name(scenario)}",
    ]
    if target_domain:
        cmd.extend(["--set", f"reconqa_domain={target_domain.strip()}"])
    return cmd


addons = [ReconQAHarAddon()] if ctx is not None else []
