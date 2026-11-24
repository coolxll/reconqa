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
  - **Isolated Per-Project Overview (`/`)**: Workflow hero banner, per-project status cards (isolated endpoints, coverage %, high/critical findings, and quick links per project—never mixed across unrelated projects), recent runs table, and actionable **Toolchain Doctor** with one-click user-space tool installation (`POST /api/v1/system/doctor/install`).
  - **Production SPA Packaging**: Multi-stage Node 22 + Nginx container (`docker/Dockerfile.frontend` + `docker/nginx.conf`) with SSE-safe reverse proxying.
- **Decoupled Explore & Discovery Workspace (`/explore`)**:
  - Paste 1 seed cURL or URL (auth headers kept in memory only, never persisted) to probe OpenAPI/Swagger specs (including SpringBoot `/v3/api-docs/swagger-config` & `/swagger-resources`), mine HTML `<script>` / `<link rel="modulepreload">` and Webpack/Vite lazy JS chunks (skipping vendor libraries), detect sibling API hosts on the same registrable root domain, and promote selected endpoints directly into a Test Run (`POST /api/v1/explore/{explore_id}/promote-to-run`).
- **Multi-Format DevTools Input & 2-Step Zero-Friction Run Creation**:
  - Import `.har` files, paste Chrome DevTools **Copy as cURL** / **Copy all as cURL** / **Copy as fetch** / Burp **Raw HTTP** / HAR JSON (`POST /api/v1/runs/{run_id}/import-text` & `reconqa import-text`), or reuse historical HARs with automatic target-host detection and mismatch fallback.
- **Progressive On-Demand Execution**:
  - New runs execute fast core Hub analysis (`["merge"]`) first, followed by incremental on-demand execution (`POST /api/v1/runs/{run_id}/execute-modules`) inside each capability tab:
    - **API Automation Tab**: Java Maven metadata, execution summary, parsed Allure results, static Allure HTML report viewer, and one-click Maven ZIP download.
    - **Performance Tab**: HAR bottleneck profiler, ECharts visualizations, and k6 ramp script/analysis.
    - **Security Tab**: Static audit findings, auth/IDOR matrix, Nuclei results, and on-demand Burp/cURL export drawer.
  - **Historical Trends (`/trends`)**: Multi-run ECharts coverage, finding severity, and run-to-run diff comparison.
- **Project Knowledge Base (`/projects`)**:
  - Per-project `knowledge_docs` storage supporting inline Markdown notes, external wiki links, file uploads (with UTF-8 text extraction up to 512 KB for `.md`/`.yaml`/`.json`/`.txt`/`.http`/etc.), tags, related API endpoints, category/keyword filtering, and downloads alongside Target scopes and reusable Auth Profiles.
- **AI Agent Collaboration Layer (Human-in-the-Loop)**:
  - Thin FastMCP server (`reconqa mcp` via `uv sync --extra mcp`) + portable Agent Skill (`skills/reconqa/SKILL.md`) sharing ReconQA's FastAPI backend and SQLite workspace so humans and AI agents collaborate seamlessly on exploration, knowledge curation, and progressive testing.

### Phase 3 — CI/CD Automation, Distributed Scale & Deep AI Enrichment (Planned)
- **CI/CD Templates & Scheduled Regression**: Ready-to-use GitHub Actions and GitLab CI workflows, scheduled regression triggers, baseline diff gates, and Slack/Webhook quality gate notifications.
- **Distributed Worker Backend**: Optional Redis/ARQ `TaskRunner` implementation alongside the PostgreSQL production profile for concurrent multi-team deployments.
- **Deep AI Test Case & Assertion Enrichment**: Automated LLM-assisted parameter synthesis and business assertion generation using Project Knowledge Base docs (`prd`, `business_rule`, `api_spec`) to fill uncovered `js-static` / `js-static-inferred` blind spots and triage failed L2 chains or security findings.
- **Enterprise Scope Guardrails & RBAC**: Configurable domain allowlists, approval workflows for active scans (`nuclei` / active discovery / mutation replay), and role-based access control.

---

## Key Architectural Decisions

1. **Explore First, Test Progressively**: Separating standalone discovery (`/explore`) from structured test runs (`/runs`) lets testers and AI agents map an unfamiliar system from a single cURL before committing to test generation—and progressive module execution (`merge` → on-demand `java_gen` / `perf` / `security`) keeps the initial feedback loop under a few seconds.
2. **Manual Testing & DevTools Exports as First-Class Inputs**: Teams can drop in Chrome DevTools HARs, paste `Copy as cURL` / `Copy as fetch` / Raw HTTP snippets, or reuse prior runs with SHA-256 deduplication—zero Playwright or proxy setup required to get value.
3. **Standalone Packaging & Zero External Path Coupling**: Core analysis and code/script generation need zero external binaries. Optional CLI tools are installed locally via `./scripts/install-tools.sh [--only <tool>]`, the Web UI Doctor modal, or Docker images; resolution follows `RECONQA_BIN_DIR` → `~/.reconqa/bin` → `./bin` → venv → `PATH`.
4. **Java 21 Automation Baseline with Externalized Data**: Generated Java projects default to Java 21 (configurable to 17+), separate tabular L1 smoke cases (`l1_smoke_endpoints.csv`) from nested JSON params/payloads, infer business success rules from 2xx samples (overridable in `reconqa.yaml`), and never fabricate cleanup endpoints when none exist in recorded traffic.
5. **Read-Only Default for Privilege / IDOR Replay**: Automated cross-account checks replay read-only endpoints by default and export mutation endpoints as Burp candidates unless explicitly enabled with `--include-mutations`.
6. **Shared Backend for Humans & AI Agents**: Exposing `reconqa mcp` as a thin client over FastAPI ensures a single source of truth—every artifact, knowledge doc, explore session, and run created by an agent is immediately inspectable and actionable in the Vue 3 Web Console.
