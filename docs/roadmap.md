# ReconQA Roadmap & Design Decisions

## Release Phases

### Phase 1 — Core Engine & Standalone Packaging (Completed)
- **CLI & Orchestration**: Typer + Rich CLI (`init`, `doctor`, `discover`, `record`, `import-har`, `import-text`, `merge`, `gen-java`, `audit`, `perf`, `pipeline`, `serve`) sharing a unified Python pipeline engine and project-level `reconqa.yaml`.
- **Traffic Input & Optional Discovery**: Multi-tool HAR importer (Chrome DevTools, Charles, Fiddler, mitmproxy) with embedded JS extraction and SHA-256 historical reuse, `mitmproxy` live scenario-switching addon for manual smoke testing, generic minified JS static endpoint analyzer, plus optional ProjectDiscovery asset recon and an optional Playwright traffic-capture helper.
- **Unified Data Hub**: HAR 1.2 path normalizer, three-tier `x-discovery-source` OpenAPI 3.0.3 merger (`har-live` > `js-static-inferred` > `js-static`), chronological data-flow correlation engine with `>60%` static constant filtering + Mermaid diagram output, and live-vs-static JS coverage diff.
- **Java API Automation Generator**: Production Maven project generator targeting Java 21 (configurable) with `TestNG 7.10`, `RestAssured 5.5`, `Allure 2.29`, `JSON Schema Draft-07`, CSV-driven L1 smoke tests with externalized JSON payloads/params, stateful L2 correlation chains with `@AfterClass` teardown, L3 unauth boundary checks, `TestExecutionListener` JSON summary, and Schemathesis L3 fuzzing.
- **Security & Performance Engines**: Static security header / `Set-Cookie` / redacted leak scanner, read-only-by-default privilege/IDOR auth matrix (`--include-mutations` opt-in), Nuclei/ZAP integration, Burp Repeater `.http` + cURL exporter, HAR performance profiler, parameterized ramping `k6` script generator (`NO_CONN_REUSE`, proxy egress, `429`/`5xx`/`p95` threshold diagnosis), and local round-robin proxy pool.
- **FastAPI Backend & Docker**: Async SQLAlchemy (SQLite/PostgreSQL), pluggable `TaskRunner` protocol with default in-process `asyncio.Queue` worker, SSE live event streaming, HAR upload & SHA-256 reuse, and standalone Docker images (`backend`, `java-runner`, `frontend`).

### Phase 2 — Vue 3 Web Console, Explore Workspace, Knowledge Base & AI Agent Layer (Completed)
- **Bilingual Vue 3 + Vite Web Console (`ZH / EN`)**:
  - **Project-Scoped Workspace (`/p/:projectId/...`) & Overview**: Workflow hero banner, isolated per-project status cards (endpoints, coverage %, P0/P1 effectiveness, runs, KB), and actionable **Toolchain Doctor** (`/system/doctor`) with one-click user-space tool installation (`POST /api/v1/system/doctor/install`).
  - **Production SPA Packaging**: Multi-stage Node 22 + Nginx container (`docker/Dockerfile.frontend` + `docker/nginx.conf`) with SSE-safe reverse proxying.
- **Decoupled Explore & Discovery Workspace (`/explore`)**:
  - Probe OpenAPI/Swagger specs (including SpringBoot `/v3/api-docs/swagger-config` & `/swagger-resources`), mine HTML `<script>` / `<link rel="modulepreload">` and Webpack/Vite lazy JS chunks (skipping vendor libraries), detect sibling API hosts, and promote selected endpoints directly into a Test Run (`POST /api/v1/explore/{explore_id}/promote-to-run`).
- **Multi-Format DevTools Input & 2-Step Zero-Friction Run Creation**:
  - Import `.har` files, paste Chrome DevTools **Copy as cURL** / **Copy all as cURL** / **Copy as fetch** / Burp **Raw HTTP** / HAR JSON (`POST /api/v1/runs/{run_id}/import-text` & `reconqa import-text`), or reuse historical HARs with automatic target-host detection and mismatch fallback.
- **Progressive On-Demand Execution & Trends**:
  - Fast core Hub analysis (`["merge"]`) followed by incremental on-demand execution (`POST /api/v1/runs/{run_id}/execute-modules`) across **API Automation**, **Performance**, and **Security** tabs, plus **Historical Trends (`/trends`)** with ECharts coverage/severity charts and Run A→B diff comparison.
- **Project Knowledge Base (`/projects`, `/p/:projectId/kb`)**:
  - Per-project `knowledge_docs` storage supporting inline Markdown notes, external wiki links, file uploads (UTF-8 indexing up to 512 KB), tags, related endpoints, and downloads alongside Target scopes and Auth Profiles.
- **AI Agent Collaboration Layer (`reconqa mcp`)**:
  - Thin FastMCP server + portable Agent Skill (`skills/reconqa/SKILL.md`) sharing ReconQA's FastAPI backend and SQLite/PostgreSQL workspace.

### Phase 2.5 — Authenticated Exploration, Deep Verification, GraphQL, Deliverables, Quality Gates & CI (Completed)
- **Authenticated Exploration, Burp Smart Paste & Scope Isolation**:
  - 8 `ExploreAuthType` modes (`none`, `profile`, `headers`, `bearer`, `cookie`, `basic`, `login_api`, `form_login`), client-side **Burp Smart Paste** (`parseAuthPaste` auto-detecting cURL, Burp Raw HTTP, and multi-line headers), **Pre-flight Auth Verify** (`POST /api/v1/explore/verify-auth`), `scope_domains` strict credential allowlist (stripping auth headers on cross-domain redirects), multi-portal discovery (`/explore-all`, `/discover-portals`), async Explore Jobs (`/explore/jobs`), and `AuthProfile` `test-login` / `extract-from-har`.
- **5-Defense Business Capability Map & Scenario `L0`–`L4` Engine**:
  - Synthesizes endpoints into `Resource × Action × Role × State` capabilities (`GET /api/v1/projects/{project_id}/capabilities`), promotes 5-case scenarios (`/capabilities/promote-scenario`), and tracks `P0`–`P3` scenarios from `L0` to `L4` (`/scenarios`) with concurrent multi-role **Role Matrix** replay (`/replay-matrix`).
- **23-Operator Deep Assertion Engine, Checkpointed Chain Runner/Resume & Zero-JDK L3 Writeback**:
  - Security-hardened assertion engine (`assertion_engine.py`) with 23 `ScenarioAssertionOperator` values (22 executable + `custom`), safe Decimal AST `var_equals` arithmetic, SSRF-hardened `json_schema`, and fail-closed read-only SQL (`sql_equals`, `sql_exists`) restricted to `ENV:RECONQA_*` / `${RECONQA_*}` (excluding `RECONQA_DATABASE_URL`).
  - Stateful **Checkpointed Chain Runner & Resume** (`POST /chains/execute`, `POST /chain-runs/{chain_run_id}/resume`) and **TryIt Replay** (`/replay`) defaulting to `sync_scenarios=True` so assertion pass/fail results write back to `ScenarioModel.assertions[].status` without requiring `mvn test` or a JDK.
- **GraphQL Operation Splitting & Built-In Dynamic Placeholders**:
  - HAR/OpenAPI/Hub/Replay/Codegen split GraphQL requests by `operationName` into virtual endpoints (`POST /graphql#<OperationName>`) while stripping `#...` before wire HTTP dispatch, and resolve `{{$timestamp}}`, `{{$uuid}}`, `{{$randInt(min,max)}}`, and `{{$isoDate}}` dynamically (preserving native JSON integers).
- **Standalone Acceptance Report & Postman Collection v2.1 Export**:
  - One-click export of self-contained HTML, Markdown, and JSON acceptance reports (`GET /api/v1/projects/{project_id}/acceptance-report`) and Postman Collection v2.1 JSON (`GET /api/v1/projects/{project_id}/export/postman`), wired into Overview, Capabilities, and Inventory views.
- **Batch Regression & Quality Gate CLI/API + Webhooks**:
  - `reconqa gate` CLI command, `POST /api/v1/projects/{project_id}/gate`, `reconqa.yaml` `gate:` config, `QualityGateCard.vue` on Run Overview & Trends, evaluating P0 chain failures, new High+ security findings, coverage drop, and P95 latency with SSRF-hardened **Feishu, DingTalk, and Slack** webhook alerts.
- **GitHub Actions CI, Frontend Vitest Suite & 22-Tool MCP Server**:
  - `.github/workflows/ci.yml` running `uv run ruff check backend tests`, `uv run pytest -v`, `npm run test` (Vitest including 100% `zh`/`en` i18n key parity), and `npm run build` (`vue-tsc --noEmit && vite build`), alongside **22 MCP tools + 3 resources**.

### Phase 3 — Distributed Scale, Scheduled Regression & Deep AI Enrichment (Planned)
- **GitLab CI Templates & Scheduled Regression Triggers**: Ready-to-use `.gitlab-ci.yml` templates and built-in cron/scheduled regression triggers inside the backend.
- **Distributed Worker Backend**: Optional Redis/ARQ `TaskRunner` implementation alongside the PostgreSQL production profile for concurrent multi-team deployments.
- **Deep AI Test Case & Assertion Enrichment**: Automated LLM-assisted parameter synthesis and business assertion generation using Project Knowledge Base docs (`prd`, `business_rule`, `api_spec`) to fill uncovered `js-static` / `js-static-inferred` blind spots and triage failed L2 chains or security findings.
- **Enterprise Scope Guardrails & RBAC**: Workspace approval workflows for active scans (`nuclei` / active discovery / mutation replay) and multi-user role-based access control.

---

## Key Architectural Decisions

1. **Explore First, Test Progressively**: Separating standalone discovery (`/explore`) from structured test runs (`/runs`) lets testers and AI agents map an unfamiliar system from a single cURL or authenticated session before committing to test generation—and progressive module execution (`merge` → on-demand `java_gen` / `perf` / `security`) keeps the initial feedback loop under a few seconds.
2. **Zero-JDK Online L3 Verification Complementing Java Maven Suites**: Online Chain Execution, Checkpoint Resume, and TryIt Replay evaluate all 23 deep assertion operators directly in Python and sync results back to `ScenarioModel` (`sync_scenarios=True`), enabling an immediate `L0`→`L4` acceptance loop without waiting for or requiring a local JDK/Maven build, while preserving full Java TestNG/RestAssured/Allure generation for CI teams that want compiled Java suites.
3. **Virtual Fragment Splitting for GraphQL (`#<OperationName>`)**: Splitting GraphQL traffic into `POST /graphql#<OperationName>` virtual endpoints preserves existing REST path normalization, inventory deduplication, 5-Defense capability clustering, and scenario assertion matching without breaking wire HTTP requests (which strip `#...` immediately before dispatch).
4. **Manual Testing & DevTools Exports as First-Class Inputs**: Teams can drop in Chrome DevTools HARs, paste `Copy as cURL` / `Copy as fetch` / Burp Raw HTTP snippets, or reuse prior runs with SHA-256 deduplication—zero Playwright or proxy setup required to get value.
5. **Standalone Packaging & Zero External Path Coupling**: Core analysis, online chain assertions, deliverable exports, and `reconqa gate` need zero external binaries. Optional CLI tools are installed locally via `./scripts/install-tools.sh [--only <tool>]`, the Web UI Doctor modal, or Docker images; resolution follows `RECONQA_BIN_DIR` → `~/.reconqa/bin` → `./bin` → venv → `PATH`.
6. **SSRF-Hardened Webhooks, Read-Only SQL Allowlists & Secret-Free Exports**: Quality gate webhooks and replay requests enforce SSRF target validation (`validate_webhook_url`), read-only SQL assertions restrict DSNs to `ENV:RECONQA_*` / `${RECONQA_*}` (blocking `RECONQA_DATABASE_URL` and any non-`SELECT` statement), `scope_domains` isolates credentials across HTTP redirects, and Postman/Acceptance exports redact raw secrets.
7. **Shared Backend for Humans & AI Agents**: Exposing `reconqa mcp` (22 tools, 3 resources) as a thin client over FastAPI ensures a single source of truth—every artifact, knowledge doc, explore session, chain run, and scenario created by an agent is immediately inspectable and actionable in the Vue 3 Web Console.
