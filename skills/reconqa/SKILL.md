---
name: reconqa
description: Collaborate with a live ReconQA server (Human-in-the-Loop) via MCP tools and CLI to explore APIs from a seed cURL, ground test design in the Project Knowledge Base, import DevTools traffic, and progressively execute API automation (Java TestNG+Allure), performance (HAR+k6), and security/IDOR checks.
---

# ReconQA AI Agent Skill (Human-in-the-Loop QA & Security)

ReconQA is a **"one-recording, multi-use"** Web/API testing platform backed by a shared FastAPI + SQLite workspace and a live Vue 3 Web Console. When you operate through ReconQA's MCP tools, every project, Knowledge Base document, Explore session, analysis run, and finding you create is immediately visible to human engineers in the Web UI.

---

## 1. When to Use MCP Tools vs. Local CLI

| Task | Preferred Interface | Why |
|---|---|---|
| Create/list projects, search or save PRDs & business rules | **MCP** (`reconqa_*_project`, `reconqa_*_knowledge`) | Persists directly to SQLite and shows up live in the Web UI Knowledge Base |
| Discover endpoints from 1 seed cURL or target URL | **MCP** (`reconqa_explore_from_seed`, `reconqa_promote_explore_to_run`) | Returns compact summaries + top 40 endpoints without blowing up agent context |
| Import pasted cURL / fetch / Raw HTTP / HAR & build OpenAPI + correlations | **MCP** (`reconqa_import_traffic_and_analyze`, `reconqa_upload_har_files`, `reconqa_get_run_summary`) | Auto-links project, runs fast `["merge"]`, exposes `reconqa://` resources |
| Trigger Java codegen, HAR/k6 performance, or security audit on demand | **MCP** (`reconqa_execute_run_modules`, `reconqa_list_findings`) | Progressive execution: run only the capability you need, when you need it |
| Execute generated Maven tests or Allure report locally | **CLI** (`mvn test`, `allure serve target/allure-results`) | Local build/runtime execution against the generated `java-tests/` workspace |
| Execute k6 load tests or check local binaries | **CLI** (`k6 run scenario_ramp.js`, `reconqa doctor`) | High-throughput local load generation and host toolchain diagnostics |
| CI/CD batch runs without a running FastAPI server | **CLI** (`reconqa pipeline --har ...`) | Stateless file-contract execution in CI pipelines |

---

## 2. Core Human-in-the-Loop Workflow

Follow this progressive 4-stage loop instead of running every heavy module up front:

### Stage 1: Explore First (Zero-Friction Surface Mapping)
Start from a single authorized Chrome DevTools `Copy as cURL` or target URL (`https://shop.example.com`):
1. Call `reconqa_explore_from_seed(seed_curl=..., mode="passive", probe_openapi_docs=True, crawl_js=True)`.
2. Review the returned `summary`, `openapi_docs_found`, `sibling_api_hosts`, and `top_endpoints` (capped to 40 for token efficiency).
3. If the discovered endpoints match the test scope, promote them with `reconqa_promote_explore_to_run(explore_id=..., modules=["merge"])` (or pass `selected_endpoints` to narrow scope).

### Stage 2: Ground in the Project Knowledge Base
Before designing test cases, assertions, or privilege-escalation checks:
1. Check existing context with `reconqa_list_knowledge(project_id=...)` or read the full Markdown digest resource `reconqa://projects/{project_id}/knowledge`.
2. Whenever the human shares PRDs, state transitions, rate limits, role matrices, or edge-case rules—or you uncover undocumented API invariants—persist them via `reconqa_add_knowledge_doc(project_id=..., title=..., doc_type="prd"|"business_rule"|"api_spec"|"test_notes", content=..., related_endpoints=[...])`.
3. Human reviewers can immediately inspect and edit those docs in **Projects -> Knowledge Base**.

### Stage 3: Ingest Real Traffic & Inspect the Unified Hub
When the user provides DevTools traffic (cURL, `fetch(...)`, Raw HTTP, or HAR JSON):
1. Call `reconqa_import_traffic_and_analyze(content=..., scenario="01_checkout", project_id=...)`.
   - This creates/reuses the project, imports the traffic, and executes the fast core `["merge"]` hub stage.
2. Inspect the unified model cleanly via MCP resources instead of parsing raw HARs:
   - `reconqa_get_run_summary(run_id=...)` -> status, coverage %, chain count, available artifacts.
   - `reconqa://runs/{run_id}/openapi` -> merged OpenAPI 3.0.3 YAML (`har-live` vs `js-static` provenance).
   - `reconqa://runs/{run_id}/correlation` -> Mermaid data-flow graph + producer->consumer variable chains.

### Stage 4: Progressive On-Demand Capability Execution
Only trigger heavy capability modules once the core hub looks accurate:
- **API Automation (Java TestNG + RestAssured + Allure)**:
  `reconqa_execute_run_modules(run_id, ["java_gen"], config={"package_name": "com.reconqa.generated", "java_version": "21"})`
- **Performance Profiling & k6 Script Generation**:
  `reconqa_execute_run_modules(run_id, ["perf"], config={"run_k6": False})`
  (Set `run_k6: True` only when authorized to send load to a non-production target.)
- **Security Audit & Dual-Account IDOR**:
  `reconqa_execute_run_modules(run_id, ["security"], config={"include_mutations": False, "run_nuclei": False})`
- **Triage Findings**:
  Call `reconqa_list_findings(run_id, category=..., severity=..., limit=30)` to inspect prioritized findings with bounded evidence payloads.

---

## 3. Playbook: AI Browser Read-Only Roaming -> HAR -> ReconQA

Use this playbook when you have browser automation (or a human co-pilot) and want rich, authentic `har-live` request/response schemas across every module of a web app without brittle UI scripts:

1. **Start ReconQA's Scenario Proxy** (or enable native browser HAR recording):
   ```bash
   uv run reconqa record --mitm --port 8080 -o ./roam_hars --scenario 01_orders
   ```
   Point the browser to proxy `http://127.0.0.1:8080`.
2. **Strict Read-Only Roaming Discipline (Non-Negotiable)**:
   - **ONLY** click navigation menus, tabs, list filters, search bars, pagination, and read-only detail views.
   - **NEVER** click mutating or destructive actions (`Delete`, `Submit`, `Pay`, `Save`, `Approve`, `Reset Password`, `Disable`).
   - **Pause for Human-in-the-Loop** immediately whenever you encounter login screens, CAPTCHAs, MFA/2FA, or SMS verification.
3. **Switch Scenario Names per Business Module**:
   Trigger lightweight proxy control URLs as you move between modules so HARs stay cleanly segmented:
   - `http://reconqa.local/scenario/01_orders` -> browse order list & detail views
   - `http://reconqa.local/scenario/02_users` -> browse users & role views
   - `http://reconqa.local/flush` -> flush the last recorded `.har` file to `./roam_hars/`
4. **Auto-Ingest via MCP**:
   Call `reconqa_upload_har_files(har_paths=["./roam_hars"], wait=True)`.
   - ReconQA auto-detects the primary host, links/creates the project, runs fast `["merge"]` (OpenAPI 3.0.3 + correlation chains + JS Coverage Diff), and surfaces the live run immediately in the Vue 3 Web Console.
   - Follow up on demand with `reconqa_execute_run_modules(run_id, ["java_gen" | "perf" | "security"])`.

---

## 4. Dynamic Privilege-Escalation & A/B IDOR Verification Pattern

When testing authorization boundaries (RBAC / horizontal & vertical IDOR) on an **authorized non-production/staging target** (e.g. `https://shop.example.com`) and you only hold a single admin or privileged credential (Account A):

1. **Confirm Scope & Rules**:
   - Read `reconqa://projects/{project_id}/knowledge` for role definitions (e.g., `admin` vs `member`) and confirm the target is a non-production test environment.
   - Inspect `reconqa://runs/{run_id}/openapi` to locate user-provisioning endpoints (e.g., `POST /api/v1/users`, `POST /api/v1/auth/login`) and resource endpoints with object IDs (`/api/v1/orders/{id}`, `/api/v1/admin/settings`).
2. **Provision Ephemeral Low-Privilege Account B**:
   - Using Account A's authorized session, create a temporary, clearly tagged low-privilege test user (for example `qa-idor-probe-<shortid>@example.com`, role `member` / read-only) and obtain its session/token.
   - Identify or create a harmless Account A-owned test resource (`resource_id_a`).
3. **Run A/B Authorization Checks**:
   - Verify **Horizontal Isolation**: Replay Account A's resource read/update endpoints using Account B's credential (`GET/PUT /api/v1/orders/{resource_id_a}`) and expect `403`/`404`.
   - Verify **Vertical Isolation**: Replay admin-only endpoints with Account B's credential and expect `403`.
   - Verify **Unauthenticated Rejection**: Replay protected endpoints with no credentials and expect `401`/`403`.
   - Persist dual-account results through ReconQA (`reconqa_execute_run_modules(run_id, ["security"], ...)` when Auth Profiles are configured in the project) and review with `reconqa_list_findings(run_id, category="security")`.
4. **Mandatory Teardown**:
   - Immediately delete or deactivate the temporary Account B and any synthetic test records created during verification.
   - Record a sanitized RBAC test summary in the Project Knowledge Base (`doc_type="test_notes"`) so humans can track verified vs. broken boundaries.

---

## 5. Security & Privacy Hygiene (Mandatory)

- **Never log, echo, store in Knowledge Base, or commit real secrets**: Always redact `Authorization` tokens, `Cookie` values, API keys, session IDs, and passwords (e.g., `Bearer <REDACTED>`) before writing docs, summaries, or test reports.
- **Respect target safety**: Default to passive discovery (`mode="passive"`), read-only IDOR replay (`include_mutations=False`), static performance profiling (`run_k6=False`), and no active scanning (`run_nuclei=False`) unless the human explicitly approves active testing against a staging/sandbox host.
- **Keep agent context lean**: Prefer `reconqa_get_run_summary`, `reconqa_list_findings`, and `reconqa://` resources over dumping raw HAR files or megabyte HTTP responses.
