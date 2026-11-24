"""Pydantic v2 domain models and API DTOs for ReconQA."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- API / Persistence DTOs ---


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    slug: str | None = Field(default=None, max_length=128)
    description: str = ""


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    name: str
    description: str
    created_at: datetime


class TargetCreate(BaseModel):
    root_domain: str
    base_url: str
    scope_rules: dict[str, Any] = Field(default_factory=dict)


class TargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    root_domain: str
    base_url: str
    scope_rules: dict[str, Any]


class AuthProfileCreate(BaseModel):
    name: str
    role_tag: Literal["account_a", "account_b", "guest"] = "account_a"
    storage_state_path: str | None = None
    extra_headers_ref: dict[str, str] = Field(default_factory=dict)


class AuthProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    name: str
    role_tag: str
    storage_state_path: str | None
    extra_headers_ref: dict[str, str]


KnowledgeDocType = Literal[
    "prd",
    "api_spec",
    "business_rule",
    "test_notes",
    "architecture",
    "other",
]
KnowledgeDocSourceKind = Literal["markdown", "file", "link"]


class KnowledgeDocCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    doc_type: KnowledgeDocType = "prd"
    source_kind: KnowledgeDocSourceKind = "markdown"
    content: str = ""
    external_url: str | None = Field(default=None, max_length=1024)
    tags: list[str] = Field(default_factory=list)
    related_endpoints: list[str] = Field(default_factory=list)


class KnowledgeDocUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    doc_type: KnowledgeDocType | None = None
    content: str | None = None
    external_url: str | None = Field(default=None, max_length=1024)
    tags: list[str] | None = None
    related_endpoints: list[str] | None = None


class KnowledgeDocOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    title: str
    doc_type: str
    source_kind: str
    content: str
    file_path: str | None = None
    original_filename: str | None = None
    external_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    related_endpoints: list[str] = Field(default_factory=list)
    size_bytes: int = 0
    created_at: datetime
    updated_at: datetime


class RunCreate(BaseModel):
    target_id: str | None = None
    target_url: str | None = None
    modules: list[str] = Field(
        default_factory=lambda: ["merge", "java_gen", "security", "perf"]
    )
    trigger_source: Literal["cli", "web", "ci"] = "web"
    config: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    module: str
    status: str
    log_path: str | None = None
    output_meta: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    target_id: str | None = None
    status: str
    trigger_source: str
    workspace_dir: str
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    summary_metrics: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    jobs: list[JobOut] = Field(default_factory=list)


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    category: str
    severity: str
    endpoint: str
    title: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    kind: str
    rel_path: str
    size_bytes: int
    sha256: str


# --- Core Hub Data-Flow & Coverage Contracts ---


class CorrelationEndpointRef(BaseModel):
    step: int
    scenario: str = "default"
    method: str
    path: str
    raw_url: str = ""
    location: Literal["path", "query", "header", "body", "response"] | None = None
    param: str | None = None
    jsonpath: str | None = None
    sample: str | None = None


class CorrelationVariable(BaseModel):
    var_name: str
    producer: CorrelationEndpointRef
    consumers: list[CorrelationEndpointRef] = Field(default_factory=list)
    confidence: float = 0.8
    filtered_reason: str | None = None


class CorrelationChainStep(BaseModel):
    step: int
    scenario: str
    method: str
    path: str
    raw_url: str
    request_headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    request_body: Any = None
    expected_status: int = 200
    extracts: dict[str, str] = Field(default_factory=dict)  # var_name -> jsonpath
    uses: dict[str, str] = Field(default_factory=dict)  # location:param -> {{var_name}}


class CorrelationChain(BaseModel):
    name: str
    scenario: str
    steps: list[CorrelationChainStep] = Field(default_factory=list)
    cleanup_step: CorrelationChainStep | None = None


class CorrelationReport(BaseModel):
    variables: list[CorrelationVariable] = Field(default_factory=list)
    chains: list[CorrelationChain] = Field(default_factory=list)
    filtered_constants: list[str] = Field(default_factory=list)
    mermaid: str = ""


# --- Module Report DTOs ---


class DiscoverySummary(BaseModel):
    target: str = ""
    mode: str = "passive"
    subdomains_count: int = 0
    historical_urls_count: int = 0
    tls_sans_count: int = 0
    katana_urls_count: int = 0
    live_hosts_count: int = 0
    spa_routes_count: int = 0
    micro_frontends: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CoveragePrefixStats(BaseModel):
    prefix: str
    total: int = 0
    covered: int = 0
    uncovered: int = 0
    coverage_pct: float = 0.0


class UncoveredEndpoint(BaseModel):
    method: str
    path: str
    source: str
    priority: Literal["high", "medium", "low"] = "medium"
    scenarios: list[str] = Field(default_factory=list)
    js_snippet: str = ""
    inferred_params: list[str] = Field(default_factory=list)


class CoverageDiffReport(BaseModel):
    total_endpoints: int = 0
    covered_count: int = 0
    uncovered_count: int = 0
    js_static_inferred_uncovered_count: int = 0
    js_static_uncovered_count: int = 0
    coverage_pct: float = 0.0
    by_prefix: list[CoveragePrefixStats] = Field(default_factory=list)
    covered_endpoints: list[dict[str, Any]] = Field(default_factory=list)
    uncovered_endpoints: list[UncoveredEndpoint] = Field(default_factory=list)


class SecurityAuditReport(BaseModel):
    hosts_audited: int = 0
    header_coverage: dict[str, Any] = Field(default_factory=dict)
    cookie_issues: list[dict[str, Any]] = Field(default_factory=list)
    secret_findings: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)


class AuthMatrixReport(BaseModel):
    tested_endpoints: int = 0
    unauth_bypass_count: int = 0
    confirmed_idor_count: int = 0
    suspected_idor_count: int = 0
    mutation_candidate_count: int = 0
    results: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)


class PerfAuditReport(BaseModel):
    total_requests: int = 0
    slowest_ttfb: list[dict[str, Any]] = Field(default_factory=list)
    slowest_total: list[dict[str, Any]] = Field(default_factory=list)
    largest_responses: list[dict[str, Any]] = Field(default_factory=list)
    uncompressed_responses: list[dict[str, Any]] = Field(default_factory=list)
    duplicate_requests: list[dict[str, Any]] = Field(default_factory=list)
    cache_stats: dict[str, Any] = Field(default_factory=dict)
    findings: list[dict[str, Any]] = Field(default_factory=list)


class JavaGenResult(BaseModel):
    output_dir: str
    package_name: str = "com.reconqa.generated"
    java_version: str = "21"
    l1_endpoints_count: int = 0
    l2_chains_count: int = 0
    l3_auth_endpoints_count: int = 0
    schemas_generated: int = 0
    payloads_generated: int = 0
    files_generated: list[str] = Field(default_factory=list)
