"""Pydantic v2 domain models and API DTOs for ReconQA."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_serializer, field_validator

# --- API / Persistence DTOs ---


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    slug: str | None = Field(default=None, max_length=128)
    description: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name", mode="before")
    @classmethod
    def _clean_project_name(cls, value: Any) -> str | None:
        if value is None:
            return None
        trimmed = str(value).strip()
        if not trimmed:
            raise ValueError("Project name must not be blank")
        return trimmed


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


class TargetUpdate(BaseModel):
    root_domain: str | None = None
    base_url: str | None = None
    scope_rules: dict[str, Any] | None = None


class BatchTargetItem(BaseModel):
    root_domain: str
    base_url: str
    portal_label: str | None = None


class BatchTargetsCreateRequest(BaseModel):
    targets: list[BatchTargetItem] = Field(default_factory=list)


class BatchTargetsCreateResponse(BaseModel):
    added: list[TargetOut] = Field(default_factory=list)
    skipped: int = 0


class DiscoveredPortal(BaseModel):
    host: str
    url: str
    status_code: int
    title: str = ""
    is_api_gateway: bool = False
    suggested_label: str = ""


class DiscoverPortalsRequest(BaseModel):
    seed_url: str | None = None


class DiscoverPortalsResponse(BaseModel):
    root_domain: str
    env_suffix: str = ""
    portals: list[DiscoveredPortal] = Field(default_factory=list)


class TargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    root_domain: str
    base_url: str
    scope_rules: dict[str, Any]


AuthType = Literal["headers", "bearer", "cookie", "basic", "login_api", "form_login"]
ExploreAuthType = Literal[
    "none", "profile", "headers", "bearer", "cookie", "basic", "login_api", "form_login"
]


class ExploreAuthConfig(BaseModel):
    auth_type: ExploreAuthType = "none"
    credential_id: str | None = None
    token: str | None = None
    token_header: str = "Authorization"
    token_prefix: str = "Bearer "
    cookie: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    username: str | None = None
    password: str | None = None
    login_url: str | None = None
    login_method: str = "POST"
    login_content_type: Literal["json", "form"] = "json"
    username_field: str = "username"
    password_field: str = "password"
    extra_fields: dict[str, Any] = Field(default_factory=dict)
    token_jsonpath: str | None = None
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    verify_url: str | None = None
    scope_domains: list[str] = Field(default_factory=list)
    save_as_profile: bool = False
    profile_name: str | None = None
    role_tag: str = "account_a"


class ExploreVerifyAuthRequest(BaseModel):
    project_id: str | None = None
    credential_id: str | None = None
    auth: ExploreAuthConfig | None = None
    verify_url: str | None = None
    target_url: str | None = None


class ExploreVerifyAuthResponse(BaseModel):
    valid: bool
    verdict: Literal["valid", "denied", "error"]
    auth_mode: str = "none"
    header_names: list[str] = Field(default_factory=list)
    status_code: int = 0
    biz_code: int | str | None = None
    redirected_to: str | None = None
    duration_ms: float = 0.0
    message: str = ""
    warnings: list[str] = Field(default_factory=list)


class AuthProfileCreate(BaseModel):
    name: str
    role_tag: str = Field(default="account_a", min_length=1, max_length=48)
    auth_type: AuthType = "headers"
    storage_state_path: str | None = None
    extra_headers_ref: dict[str, str] = Field(default_factory=dict)
    login_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("role_tag", mode="before")
    @classmethod
    def _clean_role_tag(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("role_tag must not be empty")
        return text


def _mask_header_ref_value(raw_val: str) -> str:
    val = str(raw_val or "").strip()
    if not val:
        return ""
    if val.startswith(("ENV:", "env:")) or (val.startswith("${") and val.endswith("}")):
        return val
    if len(val) <= 8:
        return "***"
    return f"{val[:4]}•••{val[-4:]}"


_SENSITIVE_LOGIN_CONFIG_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "cookie",
    "api_key",
    "api-key",
    "apikey",
    "authorization",
)
_NON_SECRET_LOGIN_CONFIG_KEYS = frozenset(
    {
        "token_header",
        "token_prefix",
        "token_jsonpath",
        "password_field",
        "password_selector",
    }
)


def _mask_login_config_recursive(obj: Any, *, mask_all_leaves: bool = False) -> Any:
    if isinstance(obj, dict):
        masked: dict[str, Any] = {}
        for k, v in obj.items():
            key_str = str(k)
            key_low = key_str.lower()
            if key_low == "extra_fields":
                masked[key_str] = _mask_login_config_recursive(v, mask_all_leaves=True)
                continue
            is_sensitive = mask_all_leaves or (
                key_low not in _NON_SECRET_LOGIN_CONFIG_KEYS
                and any(kw in key_low for kw in _SENSITIVE_LOGIN_CONFIG_KEYWORDS)
            )
            if is_sensitive and not isinstance(v, (dict, list)):
                masked[key_str] = _mask_header_ref_value("" if v is None else str(v))
            else:
                masked[key_str] = _mask_login_config_recursive(
                    v, mask_all_leaves=mask_all_leaves
                )
        return masked
    if isinstance(obj, list):
        return [
            _mask_header_ref_value("" if item is None else str(item))
            if (mask_all_leaves and not isinstance(item, (dict, list)))
            else _mask_login_config_recursive(item, mask_all_leaves=mask_all_leaves)
            for item in obj
        ]
    return obj


class AuthProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    name: str
    role_tag: str
    auth_type: str = "headers"
    storage_state_path: str | None
    extra_headers_ref: dict[str, str]
    login_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("auth_type", mode="before")
    @classmethod
    def _coerce_auth_type(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text or "headers"

    @field_validator("login_config", mode="before")
    @classmethod
    def _coerce_login_config(cls, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @field_validator("extra_headers_ref", mode="after")
    @classmethod
    def _mask_extra_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(k): _mask_header_ref_value(str(v)) for k, v in (value or {}).items()}

    @field_validator("login_config", mode="after")
    @classmethod
    def _mask_login_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _mask_login_config_recursive(value or {})


class AuthProfileTestLoginResponse(BaseModel):
    ok: bool
    auth_type: str
    header_names: list[str] = Field(default_factory=list)
    status_code: int | None = None
    message: str = ""
    profile: AuthProfileOut


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


# --- Project Master API Inventory & Inline Replay DTOs ---


class InventoryRunRef(BaseModel):
    run_id: str
    run_name: str


class InventoryEndpoint(BaseModel):
    method: str
    path: str
    summary: str = ""
    discovery_source: str = "js-static"
    host: str = ""
    custom_headers: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)
    seen_in_runs: list[InventoryRunRef] = Field(default_factory=list)
    status_codes: list[int] = Field(default_factory=list)
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    request_example: Any = None
    response_example: Any = None


class InventoryPrefixStat(BaseModel):
    prefix: str
    total: int
    covered: int
    coverage_pct: float


class ScenarioMetaSummary(BaseModel):
    id: str
    slug: str
    name: str
    priority: str = "P2"
    business_domain: str = ""
    effectiveness_level: Literal["L0", "L1", "L2", "L3", "L4"] = "L0"
    description: str = ""


class ProjectInventoryResponse(BaseModel):
    project_id: str
    project_name: str
    runs_merged_count: int
    explore_sessions_count: int
    total_endpoints: int
    har_live_count: int
    js_static_count: int
    coverage_pct: float
    scenarios: list[str] = Field(default_factory=list)
    scenario_meta: dict[str, ScenarioMetaSummary] = Field(default_factory=dict)
    by_prefix: list[InventoryPrefixStat] = Field(default_factory=list)
    endpoints: list[InventoryEndpoint] = Field(default_factory=list)


# --- First-Class Scenario Entity & Testing Effectiveness DTOs ---

ScenarioAssertionOperator = Literal[
    "equals",
    "not_equals",
    "not_empty",
    "is_empty",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "not_contains",
    "matches",
    "array_contains",
    "length_eq",
    "length_gt",
    "type_is",
    "status_code",
    "header_equals",
    "latency_lt",
    "json_schema",
    "var_equals",
    "sql_equals",
    "sql_exists",
    "custom",
]
ScenarioAssertionStatus = Literal["pending", "passed", "failed"]
ScenarioNegativeCategory = Literal[
    "auth",
    "validation",
    "boundary",
    "idempotency",
    "state_machine",
    "security",
]
ScenarioNegativeStatus = Literal["pending", "covered", "failed"]
ScenarioPriority = Literal["P0", "P1", "P2", "P3"]
ScenarioLifecycleStatus = Literal["draft", "active", "verified", "archived"]
ScenarioSource = Literal["har-auto", "manual", "ai-agent"]
ScenarioEffectivenessLevel = Literal["L0", "L1", "L2", "L3", "L4"]


class ScenarioAssertion(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str
    target_endpoint: str = ""
    jsonpath: str = ""
    operator: ScenarioAssertionOperator = "not_empty"
    expected: str = ""
    note: str = ""
    status: ScenarioAssertionStatus = "pending"
    db_dsn_ref: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _ensure_id(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text or uuid.uuid4().hex[:12]

    @field_serializer("db_dsn_ref")
    def _serialize_db_dsn_ref(self, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return _mask_header_ref_value(text)


class AssertionEvalResult(BaseModel):
    id: str = ""
    title: str = ""
    operator: str
    passed: bool
    actual: Any = None
    expected: str = ""
    message: str = ""


class ScenarioNegativeCase(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str
    category: ScenarioNegativeCategory = "validation"
    expected_behavior: str = ""
    status: ScenarioNegativeStatus = "pending"
    note: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def _ensure_id(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text or uuid.uuid4().hex[:12]


class ScenarioCreateRequest(BaseModel):
    slug: str | None = Field(default=None, max_length=160)
    name: str | None = Field(default=None, max_length=255)
    description: str = ""
    preconditions: str = ""
    business_domain: str = ""
    priority: ScenarioPriority = "P2"
    status: ScenarioLifecycleStatus = "active"
    owner: str = ""
    source: ScenarioSource = "manual"
    tags: list[str] = Field(default_factory=list)
    assertions: list[ScenarioAssertion] = Field(default_factory=list)
    negative_cases: list[ScenarioNegativeCase] = Field(default_factory=list)


class ScenarioUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    preconditions: str | None = None
    business_domain: str | None = None
    priority: ScenarioPriority | None = None
    status: ScenarioLifecycleStatus | None = None
    owner: str | None = None
    source: ScenarioSource | None = None
    tags: list[str] | None = None
    assertions: list[ScenarioAssertion] | None = None
    negative_cases: list[ScenarioNegativeCase] | None = None


class ScenarioRunRef(BaseModel):
    run_id: str
    run_name: str
    status: str
    created_at: datetime | None = None


class ScenarioResponse(BaseModel):
    id: str
    project_id: str
    slug: str
    name: str
    description: str = ""
    preconditions: str = ""
    business_domain: str = ""
    priority: ScenarioPriority = "P2"
    status: ScenarioLifecycleStatus = "active"
    owner: str = ""
    source: ScenarioSource = "manual"
    tags: list[str] = Field(default_factory=list)
    assertions: list[ScenarioAssertion] = Field(default_factory=list)
    negative_cases: list[ScenarioNegativeCase] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    endpoint_count: int = 0
    run_count: int = 0
    last_run_id: str | None = None
    last_run_name: str | None = None
    assertion_count: int = 0
    assertions_passed_count: int = 0
    negative_case_count: int = 0
    negative_cases_covered_count: int = 0
    effectiveness_level: ScenarioEffectivenessLevel = "L0"


class ScenarioDetailResponse(ScenarioResponse):
    endpoints: list[InventoryEndpoint] = Field(default_factory=list)
    runs: list[ScenarioRunRef] = Field(default_factory=list)


class ScenarioListResponse(BaseModel):
    project_id: str
    total: int
    by_priority: dict[str, int] = Field(default_factory=dict)
    by_effectiveness: dict[str, int] = Field(default_factory=dict)
    p0_p1_total: int = 0
    p0_p1_effective_count: int = 0
    p0_p1_effective_pct: float = 0.0
    scenarios: list[ScenarioResponse] = Field(default_factory=list)


# --- Business Capability Map (Resource x Action x Role x State) DTOs ---

CapabilitySlotName = Literal[
    "happy_path",
    "invalid_params",
    "illegal_state",
    "authz_role",
    "consistency_idempotency",
]
CapabilitySlotState = Literal["covered", "pending", "failed", "missing"]


class CapabilitySlotStatus(BaseModel):
    slot: CapabilitySlotName
    status: CapabilitySlotState = "missing"
    count: int = 0


class CapabilityEndpointRef(BaseModel):
    method: str
    path: str
    action: str
    role: str
    discovery_source: str
    host: str = ""
    custom_headers: dict[str, str] = Field(default_factory=dict)


class CapabilityScenarioRef(BaseModel):
    id: str
    slug: str
    name: str
    priority: str = "P2"
    effectiveness_level: str = "L0"


class BusinessCapability(BaseModel):
    key: str
    module: str
    resource: str
    display_name: str
    title_zh: str = ""
    title_en: str = ""
    is_core: bool = False
    total_endpoints: int = 0
    live_endpoints: int = 0
    write_endpoints: int = 0
    read_endpoints: int = 0
    live_pct: float = 0.0
    roles: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    observed_statuses: dict[str, list[str]] = Field(default_factory=dict)
    has_state_machine: bool = False
    priority_score: float = 0.0
    recommended_priority: ScenarioPriority = "P2"
    linked_scenarios: list[CapabilityScenarioRef] = Field(default_factory=list)
    case_matrix: list[CapabilitySlotStatus] = Field(default_factory=list)
    case_coverage_pct: float = 0.0
    endpoints: list[CapabilityEndpointRef] = Field(default_factory=list)


class CapabilityQualityMetrics(BaseModel):
    total_endpoints: int = 0
    total_capabilities: int = 0
    core_capabilities: int = 0
    live_capabilities: int = 0
    target_capabilities: int = 0
    capability_coverage_pct: float = 0.0
    stateful_capabilities: int = 0
    state_transition_covered: int = 0
    state_coverage_pct: float = 0.0
    role_matrix_cells_total: int = 0
    role_matrix_cells_covered: int = 0
    role_matrix_coverage_pct: float = 0.0
    negative_slots_total: int = 0
    negative_slots_tested: int = 0
    negative_coverage_pct: float = 0.0
    slots_passed: int = 0
    slots_failed: int = 0
    slots_pending: int = 0
    slots_missing: int = 0
    invariant_assertions_total: int = 0
    invariant_assertions_passed: int = 0
    invariant_pass_rate_pct: float = 0.0


class CapabilityModuleSummary(BaseModel):
    module: str
    capabilities: int = 0
    live_capabilities: int = 0
    endpoints: int = 0


class ProjectCapabilityMapResponse(BaseModel):
    project_id: str
    project_name: str
    metrics: CapabilityQualityMetrics
    modules: list[CapabilityModuleSummary] = Field(default_factory=list)
    capabilities: list[BusinessCapability] = Field(default_factory=list)


class PromoteCapabilityRequest(BaseModel):
    capability_key: str = Field(..., min_length=1, max_length=255)
    priority: ScenarioPriority | None = None
    name: str | None = Field(default=None, max_length=255)
    lang: Literal["zh", "en"] = "zh"


# --- Autonomous External-System Acceptance Glue DTOs ---


class ExtractCredentialRequest(BaseModel):
    run_id: str | None = None
    name: str | None = Field(default=None, max_length=128)
    role_tag: str = Field(default="account_a", min_length=1, max_length=48)
    target_host: str | None = None

    @field_validator("role_tag", mode="before")
    @classmethod
    def _clean_extract_role_tag(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("role_tag must not be empty")
        return text


class ExtractCredentialResponse(BaseModel):
    profile: AuthProfileOut
    extracted_header_names: list[str] = Field(default_factory=list)
    source_run_id: str
    source_host: str


class ExecuteExternalToolRequest(BaseModel):
    tool: Literal["java_test", "schemathesis", "nuclei", "k6"]
    credential_id: str | None = None
    base_url: str | None = None
    sync_scenarios: bool = True
    timeout_sec: int = 120


class ExecuteExternalToolResponse(BaseModel):
    executed: bool
    tool: str
    status: str
    duration_ms: float = 0.0
    summary: dict[str, Any] = Field(default_factory=dict)
    stdout_tail: str = ""
    synced_scenarios: int = 0
    synced_assertions: int = 0
    synced_negative_cases: int = 0
    error: str | None = None


class AcceptancePromptContextSummary(BaseModel):
    targets_count: int = 0
    credentials_count: int = 0
    kb_docs_count: int = 0
    coverage_pct: float = 0.0
    p0_p1_effective_pct: float = 0.0
    scenarios_needing_attention: int = 0


class AcceptancePromptResponse(BaseModel):
    project_id: str
    project_name: str
    mode: str = "full"
    lang: Literal["zh", "en"] = "zh"
    agent: str = "generic"
    prompt_markdown: str
    context_summary: AcceptancePromptContextSummary


class ReplayRequest(BaseModel):
    method: str = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    credential_id: str | None = None
    timeout_sec: float = 15.0
    assertions: list[ScenarioAssertion] | None = None
    db_dsn_ref: str | None = None
    context_vars: dict[str, Any] = Field(default_factory=dict)
    include_scenario_assertions: bool = True
    sync_scenarios: bool = True


class ReplayResponse(BaseModel):
    status_code: int
    status_text: str
    duration_ms: float
    response_headers: dict[str, str] = Field(default_factory=dict)
    body_text: str = ""
    body_json: Any = None
    content_type: str = ""
    truncated: bool = False
    error: str | None = None
    assertion_results: list[AssertionEvalResult] = Field(default_factory=list)
    synced_scenarios: int = 0
    synced_assertions: int = 0


# --- Lightweight Chain Runner & Checkpoint/Resume DTOs ---


class ChainStepInput(BaseModel):
    step: int = 1
    name: str = ""
    method: str = "GET"
    path: str = "/"
    raw_url: str = ""
    request_headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    request_body: Any = None
    expected_status: int | None = None
    extracts: dict[str, str] = Field(default_factory=dict)
    assertions: list[ScenarioAssertion] = Field(default_factory=list)


class ChainExecuteRequest(BaseModel):
    run_id: str | None = None
    chain_name: str | None = None
    chain_index: int | None = None
    steps: list[ChainStepInput] | None = None
    base_url: str | None = None
    credential_id: str | None = None
    initial_vars: dict[str, Any] = Field(default_factory=dict)
    step_assertions: dict[str, list[ScenarioAssertion]] = Field(default_factory=dict)
    include_scenario_assertions: bool = True
    sync_scenarios: bool = True
    db_dsn_ref: str | None = None
    stop_on_failure: bool = True
    timeout_sec: float = 15.0


class ChainResumeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_step_index: int | None = Field(
        default=None,
        validation_alias=AliasChoices("from_step_index", "from_step"),
    )
    override_vars: dict[str, Any] = Field(default_factory=dict)
    override_step_body: Any = None
    override_step_headers: dict[str, str] | None = None
    override_step_assertions: list[ScenarioAssertion] | None = None
    credential_id: str | None = None
    base_url: str | None = None
    db_dsn_ref: str | None = None
    sync_scenarios: bool = True
    stop_on_failure: bool = True
    timeout_sec: float = 15.0


class ChainStepResult(BaseModel):
    step_index: int
    step: int
    name: str = ""
    method: str
    url: str
    virtual_path: str = ""
    status: Literal["passed", "failed", "skipped"]
    status_code: int = 0
    duration_ms: float = 0.0
    extracted_vars: dict[str, Any] = Field(default_factory=dict)
    assertions: list[AssertionEvalResult] = Field(default_factory=list)
    body_preview: str = ""
    error: str | None = None


class ChainRunOut(BaseModel):
    chain_run_id: str
    project_id: str
    run_id: str | None = None
    chain_name: str
    chain_index: int | None = None
    batch_id: str | None = None
    credential_id: str | None = None
    db_dsn_ref: str | None = None
    status: Literal["passed", "failed"]
    total_steps: int
    completed_steps: int
    failed_step_index: int | None = None
    resumed_from_index: int | None = None
    base_url: str = ""
    context_vars: dict[str, Any] = Field(default_factory=dict)
    step_results: list[ChainStepResult] = Field(default_factory=list)
    synced_scenarios: int = 0
    synced_assertions: int = 0
    created_at: str
    updated_at: str


class ChainBatchExecuteRequest(BaseModel):
    run_id: str | None = None
    chain_indices: list[int] | None = None
    base_url: str | None = None
    credential_id: str | None = None
    initial_vars: dict[str, Any] = Field(default_factory=dict)
    include_scenario_assertions: bool = True
    sync_scenarios: bool = True
    db_dsn_ref: str | None = None
    stop_on_failure: bool = True
    timeout_sec: float = 15.0
    max_chains: int = Field(default=50, ge=1, le=200)


class ChainBatchExecuteResponse(BaseModel):
    batch_id: str
    project_id: str
    run_id: str | None = None
    total_chains: int = 0
    passed_chains: int = 0
    failed_chains: int = 0
    chain_runs: list[ChainRunOut] = Field(default_factory=list)


class ReplayMatrixRequest(BaseModel):
    method: str = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    credential_ids: list[str] | None = None
    include_anonymous: bool = True
    timeout_sec: float = 15.0


class ReplayMatrixEntry(BaseModel):
    credential_id: str | None = None
    credential_name: str
    role_tag: str
    status_code: int
    biz_code: int | str | None = None
    duration_ms: float
    body_size: int = 0
    body_preview: str = ""
    verdict: Literal["allowed", "denied", "error"]
    error: str | None = None


class ReplayMatrixResponse(BaseModel):
    method: str
    url: str
    results: list[ReplayMatrixEntry] = Field(default_factory=list)
    allowed_count: int = 0
    denied_count: int = 0
    potential_bfla_warning: bool = False


class RunCreate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
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


# --- Quality Gate & Webhook DTOs (R5) ---


class GateRuleResult(BaseModel):
    rule: str
    label: str
    passed: bool
    skipped: bool = False
    actual: float | int | None = None
    threshold: float | int | None = None
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class QualityGateMetrics(BaseModel):
    p0_chain_failures: int = 0
    p0_failed_chains: int = 0
    p0_failed_assertions: int = 0
    total_chain_runs: int = 0
    high_security_count: int = 0
    baseline_high_security_count: int | None = None
    new_high_security_count: int = 0
    current_coverage_pct: float | None = None
    baseline_coverage_pct: float | None = None
    coverage_drop_pct: float | None = None
    p95_ms: float | None = None
    p95_source: Literal["k6", "har", "none"] = "none"
    baseline_available: bool = False


class WebhookDeliveryResult(BaseModel):
    provider: Literal["feishu", "dingtalk", "slack"]
    url_masked: str
    sent: bool
    status_code: int | None = None
    error: str | None = None


class QualityGateRequest(BaseModel):
    run_id: str | None = None
    baseline_run_id: str | None = None
    batch_id: str | None = None
    execute_chains: bool = False
    chain_indices: list[int] | None = None
    credential_id: str | None = None
    base_url: str | None = None
    db_dsn_ref: str | None = None
    max_p0_chain_failures: int | None = Field(default=None, ge=0)
    max_new_high_security: int | None = Field(default=None, ge=0)
    max_coverage_drop_pct: float | None = Field(default=None, ge=0.0)
    max_p95_ms: float | None = Field(default=None, ge=0.0)
    fail_on_high_without_baseline: bool | None = None
    feishu_webhook: str | None = None
    dingtalk_webhook: str | None = None
    slack_webhook: str | None = None
    webhook_timeout_sec: float | None = Field(default=None, ge=0.5)
    notify_on_pass: bool = True


class QualityGateResult(BaseModel):
    project_id: str | None = None
    project_name: str = ""
    target_label: str = ""
    run_id: str | None = None
    baseline_run_id: str | None = None
    batch_id: str | None = None
    passed: bool
    status: Literal["passed", "failed"]
    evaluated_at: str
    rules: list[GateRuleResult] = Field(default_factory=list)
    metrics: QualityGateMetrics = Field(default_factory=QualityGateMetrics)
    summary: str = ""
    webhook_results: list[WebhookDeliveryResult] = Field(default_factory=list)
