"""Playwright-based interactive (headed) and automated (headless) scenario HAR recorders."""

from __future__ import annotations

import hashlib
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from reconqa.utils.io import ensure_dir

MAX_JS_BUNDLE_BYTES = 2 * 1024 * 1024

CAPTCHA_HINTS: tuple[str, ...] = (
    "captcha",
    "verify you are human",
    "just a moment",
    "cf-turnstile",
    "hcaptcha",
    "recaptcha",
    "access denied",
    "滑动验证",
    "人机验证",
    "安全验证",
)


def _sanitize_scenario(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._-")
    return cleaned or "01_main"


def _root_domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _host_matches(url: str, domain_filter: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    dom = domain_filter.strip().lower().lstrip("*.")
    if not host or not dom:
        return False
    return host == dom or host.endswith(f".{dom}")


def _import_sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Install optional recorder dependencies with: "
            "uv sync --extra recorder && playwright install chromium"
        ) from exc
    return sync_playwright


def _attach_js_capture(
    page: Any,
    js_dir: Path,
    domain_filter: str,
    captured_files: list[str],
    max_bundles: int = 80,
) -> None:
    seen_hashes: set[str] = set()

    def _on_response(resp: Any) -> None:
        try:
            if len(seen_hashes) >= max_bundles:
                return
            req = resp.request
            url = str(resp.url)
            if not _host_matches(url, domain_filter):
                return
            ct = str(resp.headers.get("content-type", "")).lower()
            rtype = str(req.resource_type).lower()
            if rtype != "script" and "javascript" not in ct:
                return
            body = resp.text()
            if not body or len(body) > MAX_JS_BUNDLE_BYTES:
                return
            digest = hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()
            if digest in seen_hashes:
                return
            seen_hashes.add(digest)
            ensure_dir(js_dir)
            raw_name = Path(urlparse(url).path).name or f"bundle_{digest[:8]}.js"
            safe = (
                re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._")
                or f"bundle_{digest[:8]}.js"
            )
            if not safe.lower().endswith((".js", ".mjs", ".cjs")):
                safe = f"{safe}.js"
            dest = js_dir / safe
            if dest.exists():
                dest = js_dir / f"{dest.stem}_{digest[:8]}{dest.suffix}"
            dest.write_text(body, encoding="utf-8")
            captured_files.append(str(dest))
        except Exception:
            return

    page.on("response", _on_response)


def _is_challenge_page(page: Any) -> bool:
    try:
        title = (page.title() or "").lower()
        body_text = (page.inner_text("body") or "")[:600].lower()
    except Exception:
        return False
    combined = f"{title}\n{body_text}"
    return any(hint in combined for hint in CAPTCHA_HINTS)


def _open_context(
    browser: Any,
    har_path: Path,
    *,
    storage_state: Path | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    kwargs: dict[str, Any] = {
        "record_har_path": str(har_path),
        "record_har_content": "embed",
    }
    if storage_state is not None and storage_state.is_file():
        kwargs["storage_state"] = str(storage_state)
    if extra_headers:
        kwargs["extra_http_headers"] = dict(extra_headers)
    return browser.new_context(**kwargs)


def record_interactive(
    start_url: str,
    out_dir: Path,
    *,
    storage_state: Path | None = None,
    save_storage_state: Path | None = None,
    initial_scenario: str = "01_main",
    extra_headers: dict[str, str] | None = None,
    target_domain: str | None = None,
) -> dict[str, Any]:
    """Launch headed Chromium for manual multi-scenario HAR recording with live context rotation.

    Terminal commands during recording:
      - `next <scenario_name>` (or press Enter): snapshot storage_state & rotate to a new HAR file
      - `save <path>`: export current browser storage_state.json
      - `done` / `q`: finish and flush current HAR cleanly
    """
    sync_playwright = _import_sync_playwright()
    ensure_dir(out_dir)
    js_root = ensure_dir(out_dir / "js_bundles")
    domain_filter = target_domain or _root_domain_of(start_url)

    recorded_hars: list[dict[str, Any]] = []
    scenario_idx = 1
    current_scenario = _sanitize_scenario(initial_scenario)
    current_url = start_url
    active_state_path = Path(storage_state) if storage_state else None
    saved_state_path: str | None = None

    with tempfile.TemporaryDirectory(prefix="reconqa_state_") as tmp_dir:
        tmp_state = Path(tmp_dir) / "rotated_state.json"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                while True:
                    har_path = out_dir / f"{current_scenario}.har"
                    js_files: list[str] = []
                    context = _open_context(
                        browser,
                        har_path,
                        storage_state=active_state_path,
                        extra_headers=extra_headers,
                    )
                    page = context.new_page()
                    _attach_js_capture(
                        page, js_root / current_scenario, domain_filter, js_files
                    )
                    try:
                        page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass

                    print(
                        f"\n[ReconQA Recorder] Recording scenario "
                        f"'{current_scenario}' -> {har_path}"
                    )
                    print(
                        "Commands: 'next <name>' (or Enter) to rotate scenario | "
                        "'save <path>' to export storage_state | 'done' or 'q' to finish."
                    )

                    rotate_to: str | None = None
                    finish = False
                    while True:
                        try:
                            cmd = input(f"reconqa[{current_scenario}]> ").strip()
                        except (EOFError, KeyboardInterrupt):
                            finish = True
                            break

                        low = cmd.lower()
                        if low in ("done", "q", "quit", "exit"):
                            finish = True
                            break
                        if low.startswith("save "):
                            dest = Path(cmd[5:].strip()).expanduser()
                            ensure_dir(dest.parent)
                            context.storage_state(path=str(dest))
                            saved_state_path = str(dest)
                            print(f"[ReconQA Recorder] Exported storage_state -> {dest}")
                            continue
                        if not cmd or low == "next":
                            scenario_idx += 1
                            rotate_to = f"{scenario_idx:02d}_scenario"
                            break
                        if low.startswith("next "):
                            scenario_idx += 1
                            rotate_to = _sanitize_scenario(cmd[5:].strip())
                            break
                        # Treat bare scenario name as rotation target
                        scenario_idx += 1
                        rotate_to = _sanitize_scenario(cmd)
                        break

                    try:
                        current_url = page.url or current_url
                    except Exception:
                        pass

                    if save_storage_state is not None and finish:
                        ensure_dir(save_storage_state.parent)
                        context.storage_state(path=str(save_storage_state))
                        saved_state_path = str(save_storage_state)

                    if not finish:
                        context.storage_state(path=str(tmp_state))
                        active_state_path = tmp_state

                    context.close()
                    recorded_hars.append(
                        {
                            "scenario": current_scenario,
                            "har_path": str(har_path),
                            "js_bundles": js_files,
                        }
                    )

                    if finish or rotate_to is None:
                        break
                    current_scenario = rotate_to
            finally:
                browser.close()

    return {
        "out_dir": str(out_dir),
        "scenarios": [r["scenario"] for r in recorded_hars],
        "hars": recorded_hars,
        "js_bundles_dir": str(js_root),
        "storage_state_saved": saved_state_path,
    }


def record_auto(
    start_url: str,
    out_dir: Path,
    *,
    scenario: str = "01_auto",
    max_pages: int = 8,
    extra_paths: list[str] | None = None,
    storage_state: Path | None = None,
    extra_headers: dict[str, str] | None = None,
    scenarios_config: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Headless automated scenario recorder with scroll triggers and CAPTCHA stop-guard."""
    sync_playwright = _import_sync_playwright()
    ensure_dir(out_dir)
    js_root = ensure_dir(out_dir / "js_bundles")
    domain_filter = _root_domain_of(start_url)

    recorded_hars: list[dict[str, Any]] = []
    visited_pages: list[dict[str, Any]] = []
    captcha_hit = False

    scenario_specs: list[dict[str, Any]]
    if scenarios_config:
        scenario_specs = list(scenarios_config)
    else:
        urls = [start_url]
        for ep in extra_paths or []:
            if ep and ep.strip():
                urls.append(urljoin(start_url, ep.strip()))
        scenario_specs = [
            {
                "name": scenario,
                "urls": urls,
                "bfs": True,
                "max_pages": max_pages,
                "scroll": True,
            }
        ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for idx, spec in enumerate(scenario_specs, start=1):
                if captcha_hit:
                    break
                sc_name = _sanitize_scenario(str(spec.get("name") or f"{idx:02d}_auto"))
                har_path = out_dir / f"{sc_name}.har"
                js_files: list[str] = []
                sc_max_pages = int(spec.get("max_pages") or max_pages)
                do_bfs = bool(spec.get("bfs", False))
                do_scroll = bool(spec.get("scroll", True))

                seed_urls: list[str] = []
                if isinstance(spec.get("urls"), list):
                    for u in spec["urls"]:
                        if isinstance(u, str) and u.strip():
                            seed_urls.append(urljoin(start_url, u.strip()))
                elif isinstance(spec.get("url"), str) and spec["url"].strip():
                    seed_urls.append(urljoin(start_url, spec["url"].strip()))
                if not seed_urls:
                    seed_urls = [start_url]

                context = _open_context(
                    browser,
                    har_path,
                    storage_state=storage_state,
                    extra_headers=extra_headers,
                )
                page = context.new_page()
                _attach_js_capture(page, js_root / sc_name, domain_filter, js_files)

                queue = list(seed_urls)
                seen: set[str] = set()
                sc_visited = 0

                while queue and sc_visited < sc_max_pages:
                    target = queue.pop(0)
                    norm = target.split("#", 1)[0].rstrip("/")
                    if norm in seen:
                        continue
                    seen.add(norm)
                    try:
                        try:
                            resp = page.goto(target, wait_until="networkidle", timeout=15000)
                        except Exception:
                            resp = page.goto(target, wait_until="domcontentloaded", timeout=15000)
                            time.sleep(2.0)
                        time.sleep(1.0)

                        # Stop immediately if a CAPTCHA/anti-bot gate appears (never bypass)
                        if _is_challenge_page(page):
                            captcha_hit = True
                            break

                        sc_visited += 1
                        visited_pages.append(
                            {
                                "scenario": sc_name,
                                "url": target,
                                "status": resp.status if resp else 0,
                                "title": page.title() or "",
                            }
                        )

                        if do_scroll:
                            for frac in (0.4, 0.85):
                                page.evaluate(
                                    f"window.scrollTo(0, document.body.scrollHeight * {frac})"
                                )
                                time.sleep(0.6)

                        if do_bfs:
                            links = page.evaluate(
                                "() => Array.from(document.querySelectorAll('a[href]'))"
                                ".map(a => a.href)"
                            )
                            for lk in links or []:
                                if not isinstance(lk, str):
                                    continue
                                lu = urlparse(lk)
                                if lu.scheme in ("http", "https") and _host_matches(
                                    lk, domain_filter
                                ):
                                    lk_norm = lk.split("#", 1)[0].rstrip("/")
                                    if lk_norm not in seen and lk not in queue:
                                        queue.append(lk)
                    except Exception:
                        continue

                context.close()
                recorded_hars.append(
                    {
                        "scenario": sc_name,
                        "har_path": str(har_path),
                        "js_bundles": js_files,
                    }
                )
        finally:
            browser.close()

    return {
        "out_dir": str(out_dir),
        "scenarios": [r["scenario"] for r in recorded_hars],
        "hars": recorded_hars,
        "visited_pages": visited_pages,
        "js_bundles_dir": str(js_root),
        "captcha_hit": captcha_hit,
    }
