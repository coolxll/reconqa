# ReconQA

**One-recording, multi-use Web/API testing platform** powered by **HAR 1.2 + OpenAPI 3.0.3**.

Turn a single round of manual exploratory testing, DevTools exports (`HAR 1.2`, Chrome DevTools **Copy as cURL** / **Copy all as cURL** / **Copy as fetch**, Burp/Charles **Raw HTTP**, or `mitmproxy`), or a **single seed cURL** into a unified API & data-flow hub that progressively drives **three core testing capabilities**:

1. **API Automation (Java TestNG + RestAssured + Allure)**: Ready-to-run Maven project (`TestNG 7.10` + `RestAssured 5.5` + `Allure 2.29` + `JSON Schema Draft-07`), structured into L1 CSV-driven contract smoke tests, L2 stateful correlation chains with `@AfterClass` teardown, L3 auth boundary checks, and optional Schemathesis fuzzing.
2. **Performance Testing (HAR Profiler + k6)**: HAR bottleneck profiling (TTFB, payload size, missing compression, duplicate requests, CDN/cache hit rate), parameterized ramping `k6` scripts (`NO_CONN_REUSE`, proxy egress, `429`/`5xx`/`p95` inflection analysis), and an optional local round-robin proxy pool.
3. **Security Testing (Static Audit + Auth/IDOR + Burp)**: Static security header / `Set-Cookie` / redacted leak analysis, unauthenticated replay and read-only-by-default privilege boundary checks, optional Nuclei & OWASP ZAP integration, and Burp Repeater `.http` + cURL exports.

Surrounded by:
- **Explore & Discovery Workspace (`/explore`)**: Decoupled from test runs—start from 1 seed cURL or URL to probe Swagger/OpenAPI docs, mine HTML/Webpack/Vite JS bundles, discover sibling API hosts, and selectively promote endpoints into a test run.
- **Unified Data Hub**: Standard OpenAPI 3.0.3 (`openapi.yaml` / `openapi.json`), chronological data-flow correlation graph (`correlation.json` / `correlation.mmd`), and live-vs-static JS coverage diff (`coverage_diff.json` / `coverage_diff.md`).
- **Project Knowledge Base (`/projects`)**: Store PRDs, API specs, business rules, test notes, architecture docs, uploaded files, external wiki links, target scopes, and reusable auth credentials per project.
- **AI Agent Collaboration (`reconqa mcp`)**: Thin FastMCP server + reusable Agent Skill sharing the same FastAPI backend and SQLite workspace as the Web Console for Human-in-the-Loop exploration and testing.

---

## Architecture Overview

```text
Explore (/explore) [Decoupled Discovery]
  Seed cURL / URL ──► Swagger/OpenAPI + HTML/Webpack/Vite JS Chunks + Sibling API Hosts
        │  (Select endpoints & Promote to Test Run)
        ▼
Traffic Input: Manual Testing / HAR 1.2 / DevTools cURL & fetch / Raw HTTP / mitmproxy
        │
        ▼
Unified Data Hub (HAR 1.2 → OpenAPI 3.0.3 + Data-Flow Correlation + JS Coverage Diff)
        │  (Progressive On-Demand Execution via Web Tabs, CLI, or MCP Agent)
        ├──► 1. API Automation  (Maven + TestNG + RestAssured + Allure + Schemathesis)
        ├──► 2. Performance     (HAR Profiler + Parameterized k6 Ramp + Proxy Pool)
        └──► 3. Security        (Static Audit + Auth/IDOR Checks + Nuclei/ZAP + Burp)

Shared Layer: FastAPI + SQLite/PostgreSQL + Project Knowledge Base ◄──► Web Console & AI Agents (MCP)
```

---

## Quick Start

### 1. Install Dependencies

Requires Python `>= 3.11` and [`uv`](https://docs.astral.sh/uv/) (plus Node.js `>= 20` if running the Vue 3 Web Console locally).

```bash
# Core backend + CLI + dev tools
uv sync --extra dev

# Optional extras (install only what you need):
#   recorder : mitmproxy & optional Playwright traffic-capture helper
#   fuzz     : Schemathesis OpenAPI contract fuzzer
#   mcp      : FastMCP server for AI coding/testing agents (`reconqa mcp`)
#   postgres : asyncpg driver for PostgreSQL production deployments
uv sync --extra mcp --extra recorder --extra fuzz
```

### 2. Launch Web Console & API Server (5-Minute Local Flow)

```bash
# Terminal 1: FastAPI backend + async worker (port 8000)
make serve

# Terminal 2: Vue 3 + Vite bilingual ZH/EN Web Console (port 5173, proxies /api & /healthz -> :8000)
make dev-web
```

Open **`http://localhost:5173`**:
- **Overview (`/`)**: Hero workflow guide, isolated per-project status cards, recent runs, and actionable **Toolchain Doctor** (with one-click user-space installation for supported tools).
- **Explore (`/explore`)**: Paste 1 seed cURL or URL → probe Swagger/OpenAPI, mine JS chunks & sibling hosts → promote selected endpoints to a Test Run.
- **Pipeline Runs (`/runs` & `/runs/:runId`)**: 2-step zero-friction run creation (upload `.har`, paste DevTools cURL/fetch/Raw HTTP, or reuse historical HARs) → fast Hub analysis (`merge`) → trigger **API Automation**, **Performance**, or **Security** on demand inside dedicated tabs with live SSE logs.
- **Projects & Knowledge Base (`/projects`)**: Manage project PRDs, API specs, business rules, uploaded docs, target scopes, and reusable auth profiles.
- **Historical Trends (`/trends`)**: Multi-run ECharts coverage, finding severity, and run-to-run diff comparison.

### 3. Optional External CLI Tools

Core HAR/cURL import, OpenAPI merging, correlation graphs, Java codegen, static security audits, HAR performance profiling, and k6 script generation work with **zero external binaries**. Install optional CLI tools only when you need live execution or active scanning:

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
# Initialize project config
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

# Start the MCP server for AI agents (requires `uv sync --extra mcp` and running `reconqa serve`)
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
| [User Guide](docs/user-guide.md) | 5-minute quickstart and practical end-to-end workflows (Explore from cURL, DevTools/HAR import, Knowledge Base, AI Agents, Toolchain Doctor). |
| [Architecture & Core Contracts](docs/architecture.md) | System architecture, ER schema (including `knowledge_docs`), OpenAPI 3.0.3 & `correlation.json` contracts, Java Maven layout, CLI & REST/SSE API reference, and repository layout. |
| [AI Agent & MCP Integration](docs/agent-integration.md) | Connect Claude Code, Cursor, Codex, and other MCP clients via `reconqa mcp` for Human-in-the-Loop workflows. |
| [ReconQA Agent Skill](skills/reconqa/SKILL.md) | Portable agent workflow skill teaching AI agents how to collaborate with ReconQA's Explore, Knowledge Base, and progressive test runs. |
| [Frontend Guide](frontend/README.md) | Vue 3 + Vite + TypeScript + Tailwind + ECharts + Mermaid bilingual Web Console architecture and development commands. |
| [Roadmap & Design Decisions](docs/roadmap.md) | Completed Phase 1 & Phase 2 milestones, upcoming Phase 3 roadmap, and key engineering decisions. |

---

## Development

```bash
make dev-install   # uv sync --extra dev
make lint          # uv run ruff check backend tests
make test          # uv run pytest -v
make build-web     # cd frontend && npm install && npm run build
```

## License

MIT — see [LICENSE](LICENSE).
