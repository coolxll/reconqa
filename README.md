# ReconQA

[![CI](https://github.com/coolxll/reconqa/actions/workflows/ci.yml/badge.svg)](https://github.com/coolxll/reconqa/actions/workflows/ci.yml)

**One-recording, multi-use Web/API testing platform** powered by **HAR 1.2 + OpenAPI 3.0.3**.

Turn a single round of manual exploratory testing, DevTools exports (`HAR 1.2`, Chrome DevTools **Copy as cURL** / **Copy all as cURL** / **Copy as fetch**, Burp/Charles **Raw HTTP**, or `mitmproxy`), or a **single seed cURL / authenticated portal login** into a unified API & data-flow hub that progressively drives **three core testing capabilities**:

1. **API Automation (Online Chain Runner + Zero-JDK L3 Writeback + Java TestNG/RestAssured/Allure)**:
   - Execute stateful multi-step API chains (`POST /api/v1/projects/{project_id}/chains/execute`) with **checkpoint & resume** (`/chain-runs/{chain_run_id}/resume`), **TryIt Replay** (`/replay` & multi-role `/replay-matrix`), **23 assertion operators** (including safe arithmetic and read-only SQL via `ENV:RECONQA_*`), and **4 dynamic placeholders** (`{{$timestamp}}`, `{{$uuid}}`, `{{$randInt(min,max)}}`, `{{$isoDate}}`).
   - Automatically sync assertion pass/fail results back to `ScenarioModel` L3 statuses (`sync_scenarios=True`) for a **zero-JDK L0→L4 acceptance loop**, or generate a full **Maven project** (`TestNG 7.10` + `RestAssured 5.5` + `Allure 2.29` + `JSON Schema Draft-07`) with L1/L2/L3 suites and optional Schemathesis fuzzing.
2. **Performance Testing (HAR Profiler + k6)**: HAR bottleneck profiling (TTFB, payload size, missing compression, duplicate requests, CDN/cache hit rate), parameterized ramping `k6` scripts (`NO_CONN_REUSE`, proxy egress, `429`/`5xx`/`p95` inflection analysis), and an optional local round-robin proxy pool.
3. **Security Testing (Static Audit + Auth/IDOR + Burp)**: Static security header / `Set-Cookie` / redacted leak analysis, unauthenticated replay and read-only-by-default multi-role privilege boundary checks, optional Nuclei & OWASP ZAP integration, and Burp Repeater `.http` + cURL exports.

Surrounded by:
- **Authenticated Explore & Discovery (`/explore`)**: Decoupled from test runs—supports **8 auth modes** (`none`, `profile`, `headers`, `bearer`, `cookie`, `basic`, `login_api`, `form_login`), **Burp-style Smart Paste** (`cURL` / Raw HTTP / headers), **pre-flight auth verification** (`/api/v1/explore/verify-auth`), `scope_domains` credential isolation, multi-portal discovery (`/explore-all`), Swagger/OpenAPI probing, and HTML/Webpack/Vite JS bundle mining.
- **Unified Data Hub**: Standard OpenAPI 3.0.3 (`openapi.yaml` / `openapi.json`), **GraphQL `operationName` splitting** (`POST /graphql#GetOrderList` preserved in Hub/Inventory/Capabilities and stripped on wire), chronological data-flow correlation graph (`correlation.json` / `correlation.mmd`), and live-vs-static JS coverage diff (`coverage_diff.json` / `coverage_diff.md`).
- **5-Defense Business Capability Map & L0→L4 Scenarios (`/capabilities`, `/scenarios`)**: Compresses raw endpoints into prioritized capabilities (`Resource × Action × Role × State`) across a 5-Defense matrix (Happy Path, Invalid Params, Illegal State, AuthZ/Role, Consistency/Idempotency) and promotes 5-case scenarios tracked from `L0` to `L4`.
- **Deliverables & CI Quality Gates**: One-click standalone **HTML / Markdown / JSON Acceptance Report** (`/acceptance-report`), **Postman Collection v2.1** export (`/export/postman`), and **`reconqa gate` CLI / API** (`POST /api/v1/projects/{project_id}/gate`) with **Feishu, DingTalk, and Slack** webhook notifications.
- **Project Knowledge Base (`/projects`, `/p/:projectId/kb`)**: Store PRDs, API specs, business rules, test notes, uploaded files, external wiki links, target scopes, and reusable `AuthProfile` credentials (`headers`, `bearer`, `cookie`, `basic`, `login_api`, `form_login` + `test-login` + `extract-from-har`).
- **AI Agent Collaboration (`reconqa mcp`)**: Thin FastMCP server exposing **22 MCP tools + 3 resources** + portable `skills/reconqa/SKILL.md` sharing the same FastAPI + SQLite/PostgreSQL workspace as the Web Console.

---

## Architecture Overview

```text
Explore (/explore) [Authenticated & Decoupled Discovery]
  8 Auth Modes + Burp Smart Paste + Pre-flight Verify (/explore/verify-auth)
    ──► Swagger/OpenAPI + HTML/Webpack/Vite JS Chunks + Sibling Hosts / Multi-Portal
        │  (Select endpoints & Promote to Test Run)
        ▼
Traffic Input: Manual Testing / HAR 1.2 / DevTools cURL & fetch / Burp Raw HTTP / mitmproxy
        │
        ▼
Unified Data Hub (HAR 1.2 → OpenAPI 3.0.3 + GraphQL #OperationName Split + Correlation + JS Coverage Diff)
        │  (Progressive On-Demand Execution via Web Console, CLI, or 22-Tool MCP Agent)
        ├──► 1. Capabilities & Scenarios (5-Defense Matrix + L0→L4 + 23-Op Chain Runner/Resume + L3 Writeback)
        ├──► 2. API Automation          (Zero-JDK Online Replay/Chain or Maven + TestNG + RestAssured + Allure)
        ├──► 3. Performance             (HAR Profiler + Parameterized k6 Ramp + Proxy Pool)
        ├──► 4. Security                (Static Audit + Role-Matrix IDOR + Nuclei/ZAP + Burp)
        └──► 5. Deliverables & Gates    (HTML/MD/JSON Acceptance Report + Postman v2.1 + `reconqa gate` Webhooks)

Shared Layer: FastAPI + SQLite/PostgreSQL + Knowledge Base & Auth Profiles ◄──► Vue 3 Console & MCP (22 Tools / 3 Resources)
```

---

## Quick Start

### 1. Install Dependencies

Requires Python `>= 3.11` (CI runs Python `3.12`) and [`uv`](https://docs.astral.sh/uv/) (plus Node.js `>= 20`, `22` in CI, if running or building the Vue 3 Web Console locally).

```bash
# Core backend + CLI + dev & MCP tools (include --extra mcp when running pytest or `reconqa mcp`)
uv sync --extra dev --extra mcp

# Optional extras (install only what you need):
#   recorder : mitmproxy & optional Playwright traffic-capture helper
#   fuzz     : Schemathesis OpenAPI contract fuzzer
#   postgres : asyncpg driver for PostgreSQL production deployments
uv sync --extra dev --extra mcp --extra recorder --extra fuzz
```

### 2. Launch Web Console & API Server (5-Minute Local Flow)

```bash
# Terminal 1: FastAPI backend + async worker (port 8000)
make serve

# Terminal 2: Vue 3 + Vite bilingual ZH/EN Web Console (port 5173, proxies /api & /healthz -> :8000)
make dev-web
```

Open **`http://localhost:5173`**:
- **Overview (`/` → `/p/:projectId/overview`)**: Hero workflow guide (`AI Acceptance Prompt`, `Import Traffic`, `Explore`), 5 KPI cards, one-click **Deliverable Exports** (HTML/Markdown Acceptance Report & Postman v2.1), and Toolchain readiness strip.
- **Explore (`/explore` → `/p/:projectId/explore`)**: Paste 1 seed cURL, Burp Raw HTTP, or target URL, configure any of the 8 auth modes + pre-flight **Verify Auth**, probe Swagger/OpenAPI & JS chunks, and promote selected endpoints to a Test Run.
- **Capabilities (`/capabilities` → `/p/:projectId/capabilities`)**: **5-Defense Business Capability Map** (`Resource × Action × Role × State`), one-click 5-case Scenario promotion, inline **TryIt Drawer**, and deliverable exports.
- **Inventory (`/p/:projectId/inventory`)**: Cumulative per-project endpoint inventory (`har-live`, `openapi-doc`, `js-static-inferred`, `js-static`), GraphQL `#OperationName` virtual endpoints, **Postman v2.1** & OpenAPI downloads, and inline **TryIt Replay**.
- **Scenarios (`/scenarios` → `/p/:projectId/scenarios`)**: Manage `P0`–`P3` business scenarios, `L0`–`L4` effectiveness progression, **23 assertion operators**, and read-only SQL assertions.
- **Pipeline Runs (`/runs` → `/p/:projectId/runs` & `/runs/:runId`)**: 2-step zero-friction run creation → fast Hub analysis (`merge`) → **Correlation & Checkpointed Chain Runner/Resume**, **API Automation**, **Performance**, **Security**, **Artifacts**, and embedded **Quality Gate** card.
- **Knowledge Base (`/p/:projectId/kb`) & Projects (`/projects`)**: Manage project PRDs, API specs, business rules, uploaded docs, target scopes, and reusable `AuthProfile` credentials (`test-login`, `extract-from-har`).
- **Historical Trends (`/trends` → `/p/:projectId/trends`)**: Multi-run ECharts coverage, finding severity, Run A→B diff comparison, and embedded **Quality Gate** evaluation.
- **Toolchain Doctor (`/system/doctor`)**: Inspect 14 optional tools and trigger one-click user-space installation for supported binaries.

### 3. Optional External CLI Tools

Core HAR/cURL import, OpenAPI merging, GraphQL splitting, correlation graphs, online Chain/TryIt deep assertions, Java codegen, static security audits, HAR performance profiling, Acceptance Report / Postman v2.1 exports, and `reconqa gate` work with **zero external binaries**. Install optional CLI tools only when you need live external execution or active scanning:

```bash
# Install all ProjectDiscovery tools (subfinder, urlfinder, tlsx, katana, httpx, nuclei) into ./bin
./scripts/install-tools.sh ./bin

# Or install a single tool
./scripts/install-tools.sh ./bin --only nuclei

# Inspect toolchain availability
uv run reconqa doctor
```

### 4. CLI Quick Examples

```bash
# Initialize project config (includes gate: thresholds & webhook placeholders)
uv run reconqa init -o reconqa.yaml

# Convert pasted DevTools cURL / fetch / Raw HTTP into HAR 1.2
uv run reconqa import-text ./requests.curl --scenario 01_checkout -o ./workspaces/demo_run/har

# Run end-to-end pipeline reusing existing HAR files
uv run reconqa pipeline \
  --target-url https://api.example.com \
  --har ./sample_hars/ \
  -o ./workspaces/demo_run

# Proxy recording during manual testing (switch scenarios via http://reconqa.local/scenario/<name>)
uv run reconqa record --mitm --port 8080 -o ./workspaces/demo_run/har

# Evaluate release Quality Gate (offline workspace mode, returns non-zero on failure)
uv run reconqa gate \
  --run-dir ./workspaces/demo_run \
  --baseline-dir ./workspaces/baseline_run \
  --max-p95-ms 800 \
  --json

# Evaluate Quality Gate against a live ReconQA backend & notify Slack/Feishu/DingTalk
uv run reconqa gate \
  --project-id <project_id> \
  --api-url http://127.0.0.1:8000 \
  --slack-webhook https://hooks.slack.com/services/...

# Start the 22-tool MCP server for AI agents (requires `uv sync --extra mcp` and running `reconqa serve`)
uv run reconqa mcp --api-url http://127.0.0.1:8000
```

### 5. Run Generated Java Tests & k6 Load Scripts

```bash
# Run generated Java TestNG + RestAssured + Allure suite via Docker (no local JDK/Maven needed)
docker run --rm --network host \
  -v "$(pwd)/workspaces/demo_run/java-tests:/work" -w /work \
  -e RECONQA_BASE_URI=https://api.example.com \
  -e RECONQA_AUTH_TOKEN=your-test-token \
  maven:3.9-eclipse-temurin-21 mvn test

# Run generated k6 ramping load test
k6 run \
  -e BASE_URL=https://api.example.com \
  -e AUTH_TOKEN=your-test-token \
  -e NO_CONN_REUSE=false \
  ./workspaces/demo_run/performance/k6/scenario_ramp.js
```

---

## Docker Compose

```bash
# Build and launch full-stack API + Vue 3 Nginx Web Console (http://localhost:5173)
docker compose up -d --build api frontend

# Run containerized Java + Allure test runner
JAVA_TESTS_DIR=./workspaces/demo_run/java-tests docker compose --profile runner run --rm java-runner

# Optional profiles:
#   recorder : mitmproxy scenario recorder (port 8080)
#   security : OWASP ZAP daemon (port 8090)
#   prod     : PostgreSQL 16 database (port 5432)
```

API Swagger UI is available at `http://localhost:8000/docs` and health check at `GET /healthz`.

---

## Documentation

| Document | Description |
| :--- | :--- |
| [User Guide](docs/user-guide.md) | 5-minute quickstart and practical end-to-end workflows (Authenticated Explore, Burp Smart Paste, GraphQL splitting, Chain Runner & Resume, 23 assertion operators, Zero-JDK L0→L4 writeback, Deliverable exports, `reconqa gate`, and AI Agents). |
| [Architecture & Core Contracts](docs/architecture.md) | System architecture, ER schema, OpenAPI 3.0.3 & `correlation.json` contracts, Java Maven layout, CLI & REST/SSE API reference, and repository layout. |
| [AI Agent & MCP Integration](docs/agent-integration.md) | Connect Antigravity, Claude Desktop, Cursor, and other MCP clients via `reconqa mcp` (22 tools, 3 resources) for Human-in-the-Loop workflows. |
| [ReconQA Agent Skill](skills/reconqa/SKILL.md) | Portable agent workflow skill teaching AI agents Authenticated Explore, Knowledge Base grounding, Zero-JDK L0→L4 verification, and `reconqa gate`. |
| [Frontend Guide](frontend/README.md) | Vue 3 + Vite + TypeScript + Tailwind + ECharts + Mermaid bilingual Web Console architecture and Vitest commands. |
| [Roadmap & Design Decisions](docs/roadmap.md) | Completed Phase 1–2.5 milestones (including Wave 2 CI, deliverables, and quality gates), upcoming Phase 3 roadmap, and key engineering decisions. |

---

## Development

```bash
uv sync --extra dev --extra mcp          # Install backend dev + MCP deps (required for test_mcp_server.py)
uv run ruff check backend tests          # Backend & test linting (or `make lint`)
uv run pytest -v                         # Backend & integration test suite (or `make test`)
cd frontend && npm ci && npm run test    # Frontend Vitest suite (i18n parity, auth parser, operators, reports/gate)
cd frontend && npm run build             # TypeScript check (`vue-tsc --noEmit`) + Vite production build
```

## License

MIT — see [LICENSE](LICENSE).
