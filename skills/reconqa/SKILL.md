---
name: reconqa
description: Collaborate with a live ReconQA server (Human-in-the-Loop) via 22 MCP tools and CLI to run authenticated exploration, ground test design in the Project Knowledge Base, split GraphQL traffic, execute checkpointed chains & 23-operator deep assertions with zero-JDK L0-L4 writeback, export acceptance reports & Postman v2.1, and enforce `reconqa gate`.
---

# ReconQA AI Agent Skill (Human-in-the-Loop QA & Security)

ReconQA is a **"one-recording, multi-use"** Web/API testing platform backed by a shared FastAPI + SQLite/PostgreSQL workspace and a live Vue 3 Web Console. When you operate through ReconQA's **22 MCP tools and 3 resources**, every project, Knowledge Base document, Auth Profile, Explore session, analysis run, chain checkpoint, scenario, and finding is immediately visible to human engineers in the Web UI.

---

## 1. When to Use MCP Tools vs. Local CLI

| Task | Preferred Interface | Why |
|---|---|---|
| Create/list projects, search or save PRDs & business rules | **MCP** (`reconqa_*_project`, `reconqa_*_knowledge`) | Persists directly to DB and shows up live in the Web UI Knowledge Base |
| Inspect saved credentials or discover endpoints (8 auth modes) | **MCP** (`reconqa_list_auth_profiles`, `reconqa_explore_from_seed`, `reconqa_promote_explore_to_run`) | Reuses masked `AuthProfile`s without secret echo (`credential_id` / `auth`) + returns top 40 endpoints |
| Import pasted cURL / fetch / Raw HTTP / HAR & build OpenAPI + correlations | **MCP** (`reconqa_import_traffic_and_analyze`, `reconqa_upload_har_files`, `reconqa_get_run_summary`) | Auto-links project, splits GraphQL `#<OperationName>`, runs `["merge"]`, exposes `reconqa://` resources |
| Inspect cumulative project API inventory across Explore + Runs | **MCP** (`reconqa_get_project_inventory`) | Returns merged `har-live` vs `js-static` coverage, GraphQL virtual ops, prefix stats, and filtered endpoints |
| Compress APIs into a 5-Defense Capability Map & promote 5-case scenarios | **MCP** (`reconqa_get_capability_map`, `reconqa_promote_capability_scenario`) | Prioritizes top `Resource × Action × Role × State` capabilities instead of flat-testing 1,000+ endpoints |
| Track business scenarios, P0–P3 priorities, 23-op assertions & L0–L4 status | **MCP** (`reconqa_list_scenarios`, `reconqa_upsert_scenario`) | Defines structured assertions (23 operators + `{{$...}}` dynamic vars) synced by online replay/chains or Java suites |
| Auto-extract HAR credentials & replay single endpoints with L3 writeback | **MCP** (`reconqa_extract_credential`, `reconqa_replay_endpoint`) | Server-side `AuthProfile` injection, SSRF guard, 4KB cap, 23-operator eval, and `sync_scenarios=True` L3 writeback (zero JDK) |
| Execute multi-step API chains & resume from failed checkpoints | **MCP** (`reconqa_execute_chain`, `reconqa_resume_chain`) | Runs correlation/custom chains with `{{$...}}` placeholders, `db_dsn_ref` SQL checks, step resume, and `sync_scenarios=True` |
| Execute optional external runners with auto Scenario sync-back | **MCP** (`reconqa_run_external_tool`) | Runs `java_test`, `schemathesis`, `nuclei`, or `k6` and syncs assertion/negative-case status back into Scenarios (`L0`–`L4`) |
| Trigger Java codegen, HAR/k6 performance, or security audit on demand | **MCP** (`reconqa_execute_run_modules`, `reconqa_list_findings`) | Progressive execution: run only the capability you need, when you need it |
| Export Acceptance Report (HTML/MD/JSON) & Postman Collection v2.1 | **REST API** (`GET /api/v1/projects/{project_id}/acceptance-report?format=html\|markdown\|json`, `GET /api/v1/projects/{project_id}/export/postman`) | Produces standalone stakeholder evidence reports and runnable Postman v2.1 collections |
| Enforce release Quality Gate rules & optional webhooks | **CLI / API** (`reconqa gate`, `POST /api/v1/projects/{project_id}/gate`) | Evaluates P0 chain failures, High+ security diff, coverage drop, and P95 latency; exits non-zero on failure |
| Execute generated Maven tests or k6 load tests locally | **CLI** (`mvn test`, `k6 run scenario_ramp.js`, `reconqa doctor`) | Local compiled Java / Allure / load execution and host toolchain diagnostics |
| CI/CD batch runs without a running FastAPI server | **CLI** (`reconqa pipeline --har ...`, `reconqa gate --run-dir ...`) | Stateless file-contract execution in CI pipelines |

---

## 2. Core Human-in-the-Loop Workflow

Follow this progressive 4-stage loop instead of running every heavy module up front:

### Stage 1: Explore First (Authenticated Surface Mapping)
Start from a saved project profile, authorized Chrome DevTools `Copy as cURL`, Burp `Raw HTTP`, or target URL:
1. If working inside an existing project, call `reconqa_list_auth_profiles(project_id)` first to see masked auth profiles (`id`, `name`, `role_tag`, `auth_type`, `has_storage_state`, `header_names`).
2. Call `reconqa_explore_from_seed(seed_curl=..., target_url=..., mode="passive", probe_openapi_docs=True, crawl_js=True, project_id=..., credential_id=..., auth=...)`.
   - `auth` supports all **8 modes** (`none`, `profile`, `headers`, `bearer`, `cookie`, `basic`, `login_api`, `form_login`), `scope_domains` isolation, and `save_as_profile=True`.
   - When using the REST API directly for login validation or large/multi-portal crawls, verify credentials first with `POST /api/v1/explore/verify-auth` or trigger async/multi-portal jobs via `POST /api/v1/explore/jobs` and `POST /api/v1/projects/{project_id}/explore-all`.
3. Review `summary`, `openapi_docs_found`, `sibling_api_hosts`, and `top_endpoints` (capped to 40).
4. Promote selected endpoints via `reconqa_promote_explore_to_run(explore_id=..., modules=["merge"], selected_endpoints=...)`.

### Stage 2: Ground in the Project Knowledge Base
Before designing test cases, assertions, or privilege-escalation checks:
1. Check existing context with `reconqa_list_knowledge(project_id=...)` or read the full Markdown digest resource `reconqa://projects/{project_id}/knowledge`.
2. Whenever the human shares PRDs, state transitions, rate limits, role matrices, or edge-case rules—or you uncover undocumented API invariants—persist them via `reconqa_add_knowledge_doc(project_id=..., title=..., doc_type="prd"|"business_rule"|"api_spec"|"test_notes", content=..., related_endpoints=[...])`.

### Stage 3: Ingest Real Traffic & Inspect the Unified Hub
When the user provides DevTools traffic (cURL, `fetch(...)`, Burp Raw HTTP, or HAR JSON):
1. Call `reconqa_import_traffic_and_analyze(content=..., scenario="01_checkout", project_id=...)` (or `reconqa_upload_har_files`).
   - GraphQL calls (single, batched, `application/graphql`, or GET) are automatically split by `operationName` into virtual endpoints like `POST /graphql#<OperationName>` (with `#...` stripped on wire dispatch).
2. Inspect the unified model cleanly via MCP tools/resources instead of parsing raw HARs:
   - `reconqa_get_run_summary(run_id=...)` -> status, coverage %, chain count, available artifacts.
   - `reconqa://runs/{run_id}/openapi` -> merged OpenAPI 3.0.3 YAML (`har-live`, `openapi-doc`, `js-static-inferred`, `js-static`).
   - `reconqa://runs/{run_id}/correlation` -> Mermaid data-flow graph + producer->consumer variable chains.

### Stage 4: Progressive On-Demand Verification (Fast Zero-JDK Online Path First)
1. **Fast Zero-JDK Online Assertions & Chains (Default First Step)**:
   - Run `reconqa_replay_endpoint(..., sync_scenarios=True)` or `reconqa_execute_chain(project_id, run_id=..., chain_index=0, credential_id=..., sync_scenarios=True)` (and `reconqa_resume_chain(...)` on partial/failed checkpoints) to verify business assertions live and update Scenario L3 status immediately.
2. **Heavy Offline/External Modules (On Demand)**:
   - **API Automation (`java_gen`)**: `reconqa_execute_run_modules(run_id, ["java_gen"], config={"package_name": "com.reconqa.generated", "java_version": "21"})`
   - **Performance (`perf`)**: `reconqa_execute_run_modules(run_id, ["perf"], config={"run_k6": False})` (set `run_k6: True` only when authorized on non-production)
   - **Security (`security`)**: `reconqa_execute_run_modules(run_id, ["security"], config={"include_mutations": False, "run_nuclei": False})`
   - **Triage Findings**: `reconqa_list_findings(run_id, category=..., severity=..., limit=30)`.

---

## 3. Playbook: Authenticated Playwright Roaming -> HAR -> Zero-JDK L0→L4 Loop

### Authenticated Playwright Exploration & Read-Only Roaming
1. **Check Auth Profiles First**: Call `reconqa_list_auth_profiles(project_id)` to see if a profile with `has_storage_state=True` or `credential_id` already exists (and reuse `credential_id` across explore, replay, and chain execution).
2. **Start ReconQA's Scenario Proxy** (pointing Playwright/browser to `http://127.0.0.1:8080`, loading `storage_state` if available):
   ```bash
   uv run reconqa record --mitm --port 8080 -o ./roam_hars --scenario 01_orders
   ```
3. **Strict Read-Only Roaming Discipline (Non-Negotiable)**:
   - **ONLY** click navigation menus, tabs, list filters, search bars, pagination, and read-only detail views.
   - **NEVER** click mutating or destructive actions (`Delete`, `Submit`, `Pay`, `Save`, `Approve`, `Reset Password`, `Disable`).
   - **Pause for Human-in-the-Loop** immediately whenever you encounter login screens, CAPTCHAs, MFA/2FA, or SMS verification.
4. **Segment & Ingest**: Switch scenarios via `http://reconqa.local/scenario/<name>`, flush via `http://reconqa.local/flush`, and ingest with `reconqa_upload_har_files(har_paths=["./roam_hars"], wait=True)`.

### Autonomous 0->1 Acceptance Loop (Zero-JDK Online + Optional External Runners)
1. **Surface & Traffic Capture**: Run `reconqa_explore_from_seed` and/or read-only roaming -> `reconqa_upload_har_files`.
2. **Zero-Secret Session Extraction**: Call `reconqa_extract_credential(project_id, run_id=..., role_tag="account_a")` (or reuse `reconqa_list_auth_profiles`) so tokens stay server-side.
3. **Model Capabilities & 23-Operator Deep Assertions**:
   - Call `reconqa_get_capability_map(project_id, live_only=True)` and promote top `P0`/`P1` capabilities via `reconqa_promote_capability_scenario(project_id, capability_key=...)` into 5-case scenarios.
   - Read `reconqa://projects/{project_id}/knowledge` and refine scenarios with `reconqa_upsert_scenario(...)` using ReconQA's **23 `ScenarioAssertionOperator` operators** (22 executable + `custom`):
     - `equals`, `not_equals`, `not_empty`, `is_empty`, `gt`, `gte`, `lt`, `lte`, `contains`, `not_contains`, `matches`, `array_contains`, `length_eq`, `length_gt`, `type_is`, `status_code`, `header_equals`, `latency_lt`, `json_schema`, `var_equals` (supports `{{var}}` & safe `Decimal` `+ - * /` arithmetic), `sql_equals`, `sql_exists`, `custom`.
     - For SQL assertions, pass `db_dsn_ref` **only** as `ENV:RECONQA_*` or `${RECONQA_*}` (never `RECONQA_DATABASE_URL`, single `SELECT` only).
   - Use **4 built-in dynamic placeholders** in requests/chains/assertions: `{{$timestamp}}`, `{{$uuid}}`, `{{$randInt(min,max)}}`, `{{$isoDate}}`.
4. **Zero-JDK L0→L4 Loop (Online Replay + Checkpointed Chain Runner)**:
   - Spot-check endpoints and auto-sync matching scenario assertions:
     `reconqa_replay_endpoint(project_id, method=..., url=..., credential_id=profile_id, include_scenario_assertions=True, sync_scenarios=True)`
   - Execute stateful multi-step chains and auto-sync L3 assertion statuses:
     `reconqa_execute_chain(project_id, run_id=run_id, chain_index=0, credential_id=profile_id, include_scenario_assertions=True, sync_scenarios=True)`
   - On failure, inspect `failed_step_index` / `context_vars`, patch only the failing step, and resume without re-running prior passed steps:
     `reconqa_resume_chain(project_id, chain_run_id=..., from_step_index=..., override_vars=..., sync_scenarios=True)`
   - Re-check `reconqa_list_scenarios(project_id)` to verify `p0_p1_effective_pct` advances without requiring `mvn test` or a JDK!
5. **Optional External Runners (When Compiled/Fuzz/Load Verification Is Needed)**:
   - `reconqa_run_external_tool(project_id, run_id, tool="java_test" | "schemathesis" | "nuclei" | "k6", credential_id=profile_id, sync_scenarios=True)`
6. **Deliverables & Quality Gate**:
   - Export Acceptance Report (`GET /api/v1/projects/{project_id}/acceptance-report?format=html|markdown|json`) and Postman v2.1 Collection (`GET /api/v1/projects/{project_id}/export/postman`).
   - Run `uv run reconqa gate --project-id <project_id> --api-url http://127.0.0.1:8000` (or `POST /api/v1/projects/{project_id}/gate`) to verify release thresholds before handoff.

---

## 4. Dynamic Privilege-Escalation & A/B IDOR Verification Pattern

When testing authorization boundaries (RBAC / horizontal & vertical IDOR) on an **authorized non-production/staging target** and you hold a privileged credential (Account A):

1. **Confirm Scope & Rules**: Read `reconqa://projects/{project_id}/knowledge` and `reconqa://runs/{run_id}/openapi`; confirm non-production target.
2. **Provision Ephemeral Low-Privilege Account B**: Create a clearly tagged low-privilege test user (e.g., `qa-idor-probe-<shortid>@example.com`) and record a harmless Account A-owned test resource (`resource_id_a`).
3. **Run A/B Authorization Checks**: Verify horizontal isolation (`403`/`404` on Account A resources with Account B), vertical isolation (`403` on admin endpoints), and unauthenticated rejection (`401`/`403`), or use `POST /api/v1/projects/{project_id}/replay-matrix` / `reconqa_execute_run_modules(run_id, ["security"])`.
4. **Mandatory Teardown**: Immediately delete/deactivate temporary Account B and synthetic records, and record a sanitized summary in the Knowledge Base (`doc_type="test_notes"`).

---

## 5. Security & Privacy Hygiene (Mandatory)

- **Never log, echo, store in Knowledge Base, or commit real secrets**: Always redact `Authorization` tokens, `Cookie` values, API keys, session IDs, and passwords (`Bearer <REDACTED>`).
- **Enforce `scope_domains` isolation**: Always configure `scope_domains` when exploring authenticated targets so credentials are stripped on out-of-scope hosts and cross-domain `3xx` redirects.
- **Enforce read-only SQL `db_dsn_ref` safety**: Only reference `ENV:RECONQA_*` or `${RECONQA_*}` environment variables (never plaintext DSNs and never `RECONQA_DATABASE_URL`); queries are restricted to single `SELECT` statements.
- **Respect target safety**: Default to `mode="passive"`, `include_mutations=False`, `run_k6=False`, and `run_nuclei=False` unless the human explicitly approves active testing against a staging/sandbox host.
- **Keep agent context lean**: Prefer `reconqa_get_run_summary`, `reconqa_list_findings`, `reconqa_get_capability_map`, and `reconqa://` resources over dumping raw HAR files or megabyte HTTP responses.
