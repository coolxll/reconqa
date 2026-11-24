"""ReconQA Module 3: Unified Data Hub (HAR Parser, OpenAPI Merger, Correlation, Coverage Diff)."""

from reconqa.modules.hub.correlation import analyze_correlations
from reconqa.modules.hub.coverage_diff import compute_coverage_diff
from reconqa.modules.hub.har_parser import (
    HarEntry,
    normalize_path,
    parse_har_dir,
    parse_har_file,
)
from reconqa.modules.hub.openapi_merger import merge_to_openapi

__all__ = [
    "HarEntry",
    "analyze_correlations",
    "compute_coverage_diff",
    "merge_to_openapi",
    "normalize_path",
    "parse_har_dir",
    "parse_har_file",
]
