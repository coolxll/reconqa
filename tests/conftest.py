"""Shared pytest fixtures for ReconQA test suite."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.mock_server import MockShopServer

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES_HAR_DIR = FIXTURES_DIR / "har"


@pytest.fixture(scope="session")
def fixtures_har_dir() -> Path:
    assert FIXTURES_HAR_DIR.is_dir(), f"Missing fixture HAR dir: {FIXTURES_HAR_DIR}"
    return FIXTURES_HAR_DIR


@pytest.fixture()
def work_har_dir(tmp_path: Path, fixtures_har_dir: Path) -> Path:
    """Copy fixture HARs into an isolated temp directory for tests that write alongside."""
    dest = tmp_path / "har"
    shutil.copytree(fixtures_har_dir, dest)
    return dest


@pytest.fixture()
def mock_shop_server() -> Iterator[MockShopServer]:
    with MockShopServer(idor_vulnerable=True, unauth_bypass_orders=False) as srv:
        yield srv


def rewrite_har_base_url(src_har_dir: Path, dst_har_dir: Path, base_url: str) -> Path:
    """Copy fixture HARs replacing https://shop.example.test with a local mock server URL."""
    dst_har_dir.mkdir(parents=True, exist_ok=True)
    for har_file in sorted(src_har_dir.glob("*.har")):
        raw = har_file.read_text(encoding="utf-8")
        replaced = raw.replace("https://shop.example.test", base_url.rstrip("/"))
        # Validate JSON integrity
        json.loads(replaced)
        (dst_har_dir / har_file.name).write_text(replaced, encoding="utf-8")
    return dst_har_dir
