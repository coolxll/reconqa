# ReconQA User Guide

This guide walks you through setting up ReconQA locally or with Docker and running its four core workflows:

- **Workflow A**: Explore an unfamiliar API surface from **1 Seed cURL** (`/explore`)
- **Workflow B**: Import **HAR** or paste **Chrome DevTools cURL / fetch / Raw HTTP** and progressively run **API Automation**, **Performance**, and **Security** (`/runs`)
- **Workflow C**: Organize PRDs, API specs, business rules, and reusable auth credentials in the **Project Knowledge Base** (`/projects`)
- **Workflow D**: Collaborate with **AI Agents** via `reconqa mcp` + `skills/reconqa/SKILL.md`

---

## 1. 5-Minute Quickstart

### Option A: Local Development (`uv` + Vite)

**Prerequisites**: Python `>= 3.11`, [`uv`](https://docs.astral.sh/uv/), and Node.js `>= 20`.

```bash
# 1. Install backend + CLI dependencies
uv sync --extra dev

# Optional: add MCP support for AI agents, or mitmproxy/Playwright & Schemathesis
uv sync --extra mcp --extra recorder --extra fuzz

# 2. Terminal 1 — Start FastAPI backend + async worker (port 8000)
make serve

# 3. Terminal 2 — Start Vue 3 + Vite Web Console (port 5173)
make dev-web
```

Open **`http://localhost:5173`** (switch between **中文 / EN** anytime in the top bar).

### Option B: Docker Compose

```bash
# Build and launch API (:8000) + Vue 3 Nginx Web Console (:5173)
docker compose up -d --build api frontend
```

Open **`http://localhost:5173`** (API docs at `http://localhost:8000/docs`).

> **Zero External Binaries Required to Start**: Importing HAR/cURL/fetch, building OpenAPI 3.0.3, correlation graphs, generating Java Maven projects, running static security audits, profiling HAR performance, and generating `k6` scripts all run natively in pure Python.

---

## 2. Workflow A: Explore from 1 Seed cURL (`/explore`)

When you are handed a new web app or service with incomplete docs, start in **Explore & Discovery (`/explore`)**:

1. Open Chrome DevTools → **Network** tab → right-click any authenticated XHR/fetch request to your target (for example, `https://shop.example.com/api/v1/orders`) → **Copy → Copy as cURL (bash)**.
2. In ReconQA, navigate to **Explore (`/explore`)** and paste the cURL into the **Seed cURL** input (or enter a bare **Target URL** such as `https://shop.example.com`).
3. Choose discovery options:
   - **Mode**: `Passive` (fast, safe default) or `Active` (adds `katana` / `httpx` when installed).
   - **Probe OpenAPI/Swagger Docs**: Checks common spec endpoints (`/openapi.json`, `/swagger.json`, `/v3/api-docs`, `/v2/api-docs`, `/api/v3/api-docs`, `/swagger-resources`, `/v3/api-docs/swagger-config`).
   - **Mine HTML & JS Bundles**: Fetches the landing page HTML and parses `<script>` / `<link rel="modulepreload">` tags plus Webpack/Vite lazy-loaded chunks (skipping heavy vendor bundles) to extract hidden API paths, HTTP methods, SPA routes, and sibling API hosts (e.g., `api.example.com` referenced from `shop.example.com`).
4. Click **Start Explore**.
   - *Privacy & Credential Safety*: Auth headers from your seed cURL are kept strictly in memory for probing and are **never saved** to `explore_result.json` (only header names appear in `auth_headers_detected`).
5. Review the discovered **OpenAPI specs**, **Endpoints** (tagged by source: `seed_curl`, `openapi_probe`, `js_static_inferred`, `js_static`), **Sibling API Hosts**, and **SPA Routes**.
6. Select the endpoints you want to test and click **Promote to Test Run**:
   - ReconQA automatically creates or reuses the Project & Target, carries over the seed HAR and JS bundles, synthesizes a discovered HAR for your selected endpoints, and opens the new Run in `/runs/:runId`.

---

## 3. Workflow B: Import HAR or Paste DevTools Traffic (`/runs`)

Already performed a round of manual testing in the browser or captured traffic in Charles / Fiddler / mitmproxy?

### Step 1: 2-Step Zero-Friction Run Creation

1. Go to **Pipeline Runs (`/runs`)** and click **New Analysis** (or open `/runs?new=1`).
2. **Step 1 — Provide Traffic** (choose any tab):
   - **Upload HAR**: Drag and drop one or more `.har` files exported from Chrome DevTools (*Save all as HAR with content*), Charles, Fiddler, or mitmproxy.
   - **Paste DevTools Text**: Paste a single **Copy as cURL**, **Copy all as cURL**, **Copy as fetch**, Burp/Charles **Raw HTTP**, or raw **HAR 1.2 JSON** (format is auto-detected).
   - **Reuse Historical HARs**: Pick scenarios from an earlier run in the same project (deduplicated by SHA-256).
3. **Step 2 — Project Selection**:
   - Let ReconQA **auto-create/match a project** from the detected host (e.g., `api.example.com`), create a named project inline, or attach to an existing project.
4. Click **Launch Analysis**.

ReconQA runs fast core Hub analysis (`merge`) first—usually completing in seconds—and lands you in the **Run Inspector (`/runs/:runId`)**:
- **Overview**: Live SSE execution log terminal and job status timeline.
- **OpenAPI 3.0.3**: Searchable contract explorer filterable by `x-discovery-source` (`har-live`, `js-static-inferred`, `js-static`) with request/response schema drawers and JS code snippets.
- **Correlation & Coverage**: Interactive Mermaid data-flow graph showing producer → consumer ID chains (with static constants filtered out) and live-vs-JS coverage blind spots.

### Step 2: Progressive On-Demand Capabilities

From the same Run Inspector, trigger each testing capability only when you need it (calls `POST /api/v1/runs/{run_id}/execute-modules`):

1. **API Automation Tab (`java_gen`)**:
   - Click **Generate Java Test Suite** to synthesize a ready-to-run Maven project (`com.reconqa.generated`, Java 21, `TestNG 7.10`, `RestAssured 5.5`, `Allure 2.29`, `JSON Schema Draft-07`) with L1 CSV smoke tests, L2 stateful correlation chains, and L3 auth boundary checks.
   - Download the ZIP (`java-tests.zip`) or run locally / via Docker:
     ```bash
     docker run --rm --network host \
       -v "$(pwd)/workspaces/runs/<run_id>/java-tests:/work" -w /work \
       -e RECONQA_BASE_URI=https://api.example.com \
       -e RECONQA_AUTH_TOKEN=your-test-token \
       maven:3.9-eclipse-temurin-21 mvn test
     ```
   - Inspect parsed Allure test results and execution summaries directly inside the tab.
2. **Performance Tab (`perf`)**:
   - Click **Run Performance Analysis** to profile HAR bottlenecks (slow TTFB, oversized payloads, missing gzip/br compression, duplicate requests, cache hit rate) and generate a parameterized ramping `k6` script (`performance/k6/scenario_ramp.js`).
   - Run load tests against your staging environment:
     ```bash
     k6 run \
       -e BASE_URL=https://api.example.com \
       -e AUTH_TOKEN=your-test-token \
       -e NO_CONN_REUSE=false \
       ./workspaces/runs/<run_id>/performance/k6/scenario_ramp.js
     ```
3. **Security Tab (`security`)**:
   - Click **Run Security Audit** to check missing security headers, insecure `Set-Cookie` flags, redacted secret/PII leaks, and (when Auth Profiles are configured) read-only-by-default privilege/IDOR boundaries.
   - Open the **Burp Export Drawer** to generate and download Burp Repeater `.http` files and reproducible `cURL` commands for manual verification.

### CLI Equivalent

Prefer the terminal or scripting? Every step is available via CLI:

```bash
# Import pasted DevTools text from file or stdin
uv run reconqa import-text ./devtools.curl --scenario 01_checkout -o ./workspaces/demo/har

# Or run the full pipeline directly from HAR files
uv run reconqa pipeline \
  --target-url https://api.example.com \
  --har ./sample_hars/ \
  --modules merge,java_gen,perf,security \
  -o ./workspaces/demo
```

---

## 4. Workflow C: Project Knowledge Base & Credentials (`/projects`)

Navigate to **Projects & Knowledge Base (`/projects`)** to build long-lived testing context for each application:

1. **Knowledge Base Tab (Default)**:
   - **New Doc / Link**: Write inline Markdown notes (e.g., checkout state machine rules, edge cases, test data constraints) or link external wiki/PRD URLs.
   - **Upload File**: Upload PRDs, OpenAPI/Swagger YAML/JSON, SQL schemas, GraphQL definitions, or `.http` collections. Text-based formats (`.md`, `.txt`, `.yaml`, `.yml`, `.json`, `.csv`, `.http`, `.xml`, `.sql`, `.graphql`) are automatically indexed (up to 512 KB) for instant full-text search.
   - Filter by category (`PRD`, `API Spec`, `Business Rules`, `Test Notes`, `Architecture`, `Other`) or keyword, tag docs with `related_endpoints`, and download originals or Markdown exports.
   - Both human testers and AI Agents (via MCP) can query and update these docs so domain knowledge compounds across test cycles.
2. **Targets Tab**:
   - Define `root_domain`, `base_url`, and optional `scope_rules` for the project.
3. **Auth Profiles Tab**:
   - Register reusable `account_a`, `account_b`, or `guest` profiles and upload Playwright-format `storage_state.json` files (used for authenticated replays and cross-account privilege boundary checks).

---

## 5. Workflow D: AI Agent Collaboration (`reconqa mcp`)

ReconQA ships a thin **FastMCP Server** (`reconqa mcp`) and a portable **Agent Skill** (`skills/reconqa/SKILL.md`) so AI coding/testing agents (Claude Code, Cursor, Codex, etc.) can work alongside you in the same workspace:

1. Install the MCP extra and start the FastAPI backend:
   ```bash
   uv sync --extra mcp
   make serve
   ```
2. Register `reconqa mcp` in your MCP client (defaults to `stdio` transport talking to `http://127.0.0.1:8000`; also supports `--transport streamable-http` and `--transport sse`).
3. Load [skills/reconqa/SKILL.md](../skills/reconqa/SKILL.md) into your agent so it follows the Human-in-the-Loop workflow (querying the Project Knowledge Base, running `/explore`, promoting endpoints to a run, triggering modules progressively, and handing you the Web Console link for visual inspection).

For full client configuration snippets, tool lists, and prompt recipes, see **[AI Agent & MCP Integration](agent-integration.md)**.

---

## 6. Toolchain Doctor & Optional Tool Installation

ReconQA tracks 14 optional external tools across discovery, recording, Java execution, load testing, fuzzing, and security scanning.

### Inspect Status

- **Web Console**: Open **Overview (`/`)** and inspect the **Toolchain Readiness** panel. Click any tool card to see its exact purpose, impact if missing, copyable install command, and Docker alternative.
- **CLI**:
  ```bash
  uv run reconqa doctor
  ```

### Install Tools

1. **One-Click Install in Web UI**:
   Safe, user-space tools (`subfinder`, `urlfinder`, `tlsx`, `katana`, `httpx`, `nuclei`, `mitmdump`, `schemathesis`) can be installed directly from the Toolchain Doctor modal without `sudo` (`POST /api/v1/system/doctor/install`).
2. **Install via Script**:
   ```bash
   # Install all ProjectDiscovery binaries into ./bin
   ./scripts/install-tools.sh ./bin

   # Install a single ProjectDiscovery tool
   ./scripts/install-tools.sh ./bin --only nuclei
   ```
3. **Python Extras**:
   ```bash
   uv sync --extra recorder   # mitmproxy + optional Playwright traffic capture
   uv sync --extra fuzz       # Schemathesis OpenAPI fuzzing
   uv sync --extra mcp        # FastMCP server for AI agents
   uv sync --extra postgres   # asyncpg for PostgreSQL
   ```
4. **Docker Alternatives (No Local JDK / Maven / Allure / k6 / ZAP Needed)**:
   - Java TestNG + Allure: `JAVA_TESTS_DIR=./workspaces/runs/<run_id>/java-tests docker compose --profile runner run --rm java-runner`
   - k6 Load Test: `docker run --rm -i grafana/k6 run - < workspaces/runs/<run_id>/performance/k6/scenario_ramp.js`
   - mitmproxy Recorder: `docker compose --profile recorder up -d mitmproxy`
   - OWASP ZAP: `docker compose --profile security up -d zap`

> **Binary Resolution Order**: ReconQA resolves external binaries in strict standalone order: `RECONQA_BIN_DIR` → `~/.reconqa/bin` → `./bin` → current Python environment → system `PATH`.
