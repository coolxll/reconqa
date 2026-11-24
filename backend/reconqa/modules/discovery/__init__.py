"""Module 1: Asset & API Discovery Engine (ProjectDiscovery wrappers + SPA/MFE parser)."""

from reconqa.modules.discovery.runner import run_discovery
from reconqa.modules.discovery.spa_parser import extract_spa_routes

__all__ = ["run_discovery", "extract_spa_routes"]
