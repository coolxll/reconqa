# ReconQA User Guide

This guide walks you through setting up ReconQA locally or with Docker and running its five core workflows:

- **Workflow A**: Explore an unfamiliar API surface from **1 Seed cURL or Authenticated Session** (`/explore`)
- **Workflow B**: Import **HAR** or paste **Chrome DevTools cURL / fetch / Raw HTTP**, split **GraphQL** operations, run **Checkpointed Chains** & **TryIt Replay** (with **zero-JDK L3 writeback**), and trigger **API Automation**, **Performance**, and **Security** (`/runs`)
- **Workflow C**: Organize PRDs, API specs, **Auth Profiles**, **5-Defense Capabilities**, and **L0→L4 Scenarios** (`/projects`, `/capabilities`, `/scenarios`)
- **Workflow D**: Collaborate with **AI Agents** via `reconqa mcp` (22 tools, 3 resources) + `skills/reconqa/SKILL.md`
- **Workflow E**: Export **Acceptance Reports** (HTML / Markdown / JSON) & **Postman Collection v2.1**, and enforce **CI Quality Gates** (`reconqa gate`)

---

## 1. 5-Minute Quickstart

### Option A: Local Development (`uv` + Vite)

**Prerequisites**: Python `>= 3.11` (3.12 recommended), [`uv`](https://docs.astral.sh/uv/), and Node.js `>= 20` (22 recommended).

```bash
# 1. Install backend + CLI + dev + MCP dependencies (required for full pytest & `reconqa mcp`)
uv sync --extra dev --extra mcp

# Optional: add mitmproxy/Playwright recorder or Schemathesis fuzzer
uv sync --extra dev --extra mcp --extra recorder --extra fuzz

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

> **Zero External Binaries Required (Including Zero-JDK L3 Verification)**: Importing HAR/cURL/fetch/Raw HTTP, building OpenAPI 3.0.3, splitting GraphQL operations, running Checkpointed Chains & TryIt Replay with 23-operator deep assertions (`sync_scenarios=True` L3 writeback), generating Java Maven projects, running static security audits, profiling HAR performance, generating `k6` scripts, exporting Acceptance Reports / Postman v2.1, and running `reconqa gate` all run natively in pure Python without needing a local JDK, Maven, or `k6`.

---

## 2. Workflow A: Authenticated Explore & Discovery (`/explore`)

When you are handed a new web app or service with incomplete docs, start in **Explore & Discovery (`/explore`)**:

1. **Choose Input**: Paste a Chrome DevTools **Copy as cURL (bash/cmd)** or Burp **Raw HTTP** snippet into the Seed input, or enter a bare **Target URL** (such as `https://shop.example.com`).
2. **Configure Authentication (8 Modes + Burp Smart Paste)**:
   - Supported `ExploreAuthType` modes (`auth_type`):
     - `none` — Unauthenticated surface probe
     - `profile` — Reuse an existing project `AuthProfile` by `credential_id`
     - `headers` — Custom header map (e.g., `X-API-Key`, `X-Tenant-Id`)
     - `bearer` — Token with configurable `token_header` (`Authorization`) and `token_prefix` (`Bearer `)
     - `cookie` — Raw `Cookie` header string
     - `basic` — HTTP Basic auth (`username` / `password`)
     - `login_api` — Execute a pre-flight JSON or form login request (`login_url`, `login_method`, `login_content_type`, `username_field`, `password_field`, `extra_fields`, `token_jsonpath`)
     - `form_login` — Playwright headless browser form login (`login_url`, `username_selector`, `password_selector`, `submit_selector`)
   - **Burp-Style Smart Paste**: In the Web Console's Auth Smart Paste box, paste any bash/cmd `cURL` (including `-b`, `-u`, `-d`, `$'...'`), Burp/Charles **Raw HTTP** request, or multi-line HTTP headers. ReconQA's `parseAuthPaste` automatically strips noisy browser headers (`sec-ch-*`, `sec-fetch-*`, etc.) and classifies/populates `login_api`, `basic`, `bearer`, `cookie`, or `headers` fields.
   - **Pre-flight Auth Verify**: Click **Verify Auth** (`POST /api/v1/explore/verify-auth`) before crawling to confirm your session returns `valid` (HTTP 2xx + non-denied `biz_code`, not `denied` or `error`).
   - **Credential Isolation & Persistence**:
     - Set `scope_domains` (e.g. `["api.example.com", "shop.example.com"]`) so ReconQA strictly restricts credential headers to those domains and strips auth headers on any cross-domain `3xx` redirect.
     - Optionally check **Save as Profile** (`save_as_profile=True`, `profile_name`, `role_tag`) to persist the verified session into the project's `AuthProfile` store (raw secrets are never written to `explore_result.json`).
3. **Choose Discovery Scope**:
   - **Mode**: `passive` (fast, safe default) or `active` (adds `katana` / `httpx` when installed).
   - **Probe OpenAPI/Swagger Docs**: Checks `/openapi.json`, `/swagger.json`, `/v3/api-docs`, `/v2/api-docs`, `/api/v3/api-docs`, `/swagger-resources`, `/v3/api-docs/swagger-config`.
   - **Mine HTML & JS Bundles**: Parses `<script>` / `<link rel="modulepreload">` tags and Webpack/Vite lazy-loaded chunks (skipping heavy vendor bundles) to extract hidden API paths, SPA routes, and sibling API hosts.
   - **Async Jobs & Multi-Portal Explore**: Run long discoveries asynchronously via `POST /api/v1/explore/jobs` (poll `GET /api/v1/explore/jobs` or `/explore/jobs/{job_id}`), discover sibling portals (`POST /api/v1/projects/{project_id}/discover-portals`), or explore all project targets at once (`POST /api/v1/projects/{project_id}/explore-all`).
4. **Review & Promote to Test Run**:
   - Inspect discovered **OpenAPI specs**, **Endpoints** (`seed_curl`, `openapi_probe`, `js_static_inferred`, `js_static`), **Sibling API Hosts**, and **SPA Routes**.
   - Select endpoints and click **Promote to Test Run** (`POST /api/v1/explore/{explore_id}/promote-to-run`).

---

## 3. Workflow B: Traffic Import, GraphQL Splitting, Chain Runner & L3 Writeback (`/runs`)

Already performed manual testing in the browser or captured traffic in Charles / Fiddler / mitmproxy?

### Step 1: 2-Step Zero-Friction Run Creation

1. Go to **Pipeline Runs (`/runs`)** and click **New Analysis** (or `/runs?new=1`).
2. **Step 1 — Provide Traffic**:
   - **Upload HAR**: Drag and drop `.har` files exported from Chrome DevTools, Charles, Fiddler, or mitmproxy.
   - **Paste DevTools Text**: Paste **Copy as cURL**, **Copy all as cURL**, **Copy as fetch**, Burp/Charles **Raw HTTP**, or **HAR 1.2 JSON** (auto-detected).
   - **Reuse Historical HARs**: Pick scenarios from an earlier run in the same project (deduplicated by SHA-256).
3. **Step 2 — Project Selection**: Auto-create/match from the detected host, create a new project, or attach to an existing project, then click **Launch Analysis**.

### GraphQL Endpoint Splitting (`POST /graphql#<OperationName>`)

When HAR files or OpenAPI specs contain GraphQL calls (single JSON payloads, batched GraphQL arrays, `application/graphql`, or `GET` query params), ReconQA automatically extracts `operationName` (or parses the named operation from `query`) and splits them into distinct virtual endpoints such as `POST /graphql#GetOrderList` and `POST /graphql#CreateOrder` instead of collapsing everything under `POST /graphql`.
- Virtual `#<OperationName>` fragments are preserved across OpenAPI paths (`x-graphql-operation`, `x-graphql-type`), Master Inventory, Capability Map, and Scenario assertions.
- Whenever ReconQA sends a live HTTP request (TryIt Replay, Chain Runner, or generated Java tests), it automatically strips `#...` before dispatching over the wire.

### Step 2: Run Inspector, Chain Runner & Zero-JDK L3 Writeback

Fast core Hub analysis (`merge`) lands you in **Run Inspector (`/runs/:runId`)** with seven tabs (`Overview`, `OpenAPI`, `Correlation`, `API Automation`, `Performance`, `Security`, `Artifacts`) plus an embedded **Quality Gate** card on `Overview`:

- **Overview**: Live SSE execution terminal, job timeline, and the **Quality Gate Card** (`POST /api/v1/projects/{project_id}/gate`).
- **OpenAPI 3.0.3**: Searchable contract explorer filterable by `x-discovery-source` (`har-live`, `openapi-doc`, `js-static-inferred`, `js-static`).
- **Correlation & Checkpointed Chain Runner**:
  - Inspect the interactive Mermaid data-flow graph (producer → consumer variable chains with static constants filtered out).
  - Open any chain's **Chain Runner & Checkpoint/Resume** drawer (`POST /api/v1/projects/{project_id}/chains/execute`):
    - Select an `AuthProfile` (`credential_id`), override `base_url`, set `initial_vars`, or provide an optional read-only `db_dsn_ref` (`ENV:RECONQA_*`).
    - Toggle **Sync Results to Scenario L3** (`sync_scenarios=True` by default, `include_scenario_assertions=True` by default): matching `ScenarioModel` assertions automatically update to `passed` / `failed` in the database without running `mvn test` or needing a JDK.
    - **Checkpoint & Resume** (`POST /api/v1/projects/{project_id}/chain-runs/{chain_run_id}/resume`): If step *N* fails, fix the payload (`override_step_body`), headers (`override_step_headers`), assertions (`override_step_assertions`), or context variables (`override_vars`), and resume directly from `from_step_index` (alias `from_step`) without re-executing earlier passed steps.
- **TryIt Replay & Role Matrix**:
  - From **Inventory**, **Capabilities**, or **Scenarios**, open the **TryIt Drawer** (`POST /api/v1/projects/{project_id}/replay`) with `runAssertions` and `syncScenarios` (`sync_scenarios=True`, `include_scenario_assertions=True`) enabled to spot-check an endpoint and immediately write back L3 assertion results (`synced_scenarios` / `synced_assertions`).
  - Use **Run Role Matrix** (`POST /api/v1/projects/{project_id}/replay-matrix`) to replay an endpoint concurrently across multiple `AuthProfile` roles plus an anonymous request for BFLA/IDOR verification.

### 23 Assertion Operators & Built-In Dynamic Placeholders

Both **Chain Runner** and **TryIt Replay** (plus Scenarios) evaluate **23 `ScenarioAssertionOperator` operators** (22 executable + `custom`):

| Category | Operators | Notes |
|---|---|---|
| **Equality & Presence (4)** | `equals`, `not_equals`, `not_empty`, `is_empty` | Supports JSONPath/dot-path targets (e.g. `$.data.id`, `$.items[0].status`). |
| **Numeric Comparisons (4)** | `gt`, `gte`, `lt`, `lte` | Compares numeric response fields against expected thresholds. |
| **String, Array & Type (7)** | `contains`, `not_contains`, `matches`, `array_contains`, `length_eq`, `length_gt`, `type_is` | Regex (`matches`), element membership (`array_contains`), length checks, JSON type assertions. |
| **HTTP Contract & SLA (4)** | `status_code`, `header_equals`, `latency_lt`, `json_schema` | `status_code` accepts `200`, `200,201`, or `2xx`; `json_schema` blocks external `$ref` SSRF. |
| **Cross-Step Arithmetic (1)** | `var_equals` | Evaluates `{{var}}` references and safe Decimal AST arithmetic (`+`, `-`, `*`, `/`). |
| **Read-Only SQL (2)** | `sql_equals`, `sql_exists` | Fail-closed read-only DB assertions via `db_dsn_ref`. |
| **Manual Checklist (1)** | `custom` | Records human/manual verification notes (`passed=True`). |

- **Read-Only SQL Safety (`db_dsn_ref`)**: `db_dsn_ref` **must** reference an environment variable using `ENV:RECONQA_*` or `${RECONQA_*}` (plaintext DSNs are rejected, and `RECONQA_DATABASE_URL` plus ReconQA's internal DB are strictly blocked). Only single `SELECT` statements are permitted (no `;`, comments, or write keywords), `{{var}}` values are bound safely as SQLAlchemy bind parameters, sessions run in read-only mode (`PRAGMA query_only = ON` / `SET TRANSACTION READ ONLY`), and results are capped at 50 rows.
- **4 Built-In Dynamic Placeholders**: Use these in URLs, headers, query params, JSON bodies, or assertion expected values (even when `context_vars` is empty):
  - `{{$timestamp}}` — Current UTC epoch seconds (`int`; preserved as native `int` when used as an exact JSON value)
  - `{{$uuid}}` — Random UUIDv4 string
  - `{{$randInt(min,max)}}` — Inclusive random integer between `min` and `max` (preserved as native `int` when used as an exact JSON value)
  - `{{$isoDate}}` — Current UTC ISO-8601 timestamp ending in `Z`

### Step 3: Progressive On-Demand Heavy Modules

From the Run Inspector, trigger optional heavy modules whenever needed (`POST /api/v1/runs/{run_id}/execute-modules`):

1. **API Automation Tab (`java_gen`)**: Synthesize a Maven project (`com.reconqa.generated`, Java 21, `TestNG 7.10`, `RestAssured 5.5`, `Allure 2.29`, `JSON Schema Draft-07`) with L1 CSV smoke tests, L2 correlation chains, and L3 auth checks. Download `java-tests.zip` or run via Docker (`maven:3.9-eclipse-temurin-21 mvn test`).
2. **Performance Tab (`perf`)**: Profile HAR bottlenecks (TTFB, payload size, compression, duplicate requests, cache hit rate) and generate `performance/k6/scenario_ramp.js`.
3. **Security Tab (`security`)**: Check missing security headers, insecure `Set-Cookie` flags, redacted secret/PII leaks, multi-role IDOR boundaries, and export Burp Repeater `.http` + `cURL` commands.

### CLI Equivalent

```bash
# Import pasted DevTools text from file or stdin
uv run reconqa import-text ./devtools.curl --scenario 01_checkout -o ./workspaces/demo/har

# Run the offline pipeline directly from HAR files
uv run reconqa pipeline \
  --target-url https://api.example.com \
  --har ./sample_hars/ \
  --modules merge,java_gen,perf,security \
  -o ./workspaces/demo
```

---

## 4. Workflow C: Knowledge Base, Auth Profiles, Capabilities & Scenarios

### Project Knowledge Base, Targets & Auth Profiles (`/projects`, `/p/:projectId/kb`, `/p/:projectId/settings`)

1. **Knowledge Base**: Write inline Markdown docs (`prd`, `api_spec`, `business_rule`, `test_notes`, `architecture`, `other`), link external wikis, or upload files (`.md`, `.txt`, `.yaml`, `.yml`, `.json`, `.csv`, `.http`, `.xml`, `.sql`, `.graphql` indexed up to 512 KB for full-text search).
2. **Targets**: Configure `root_domain`, `base_url`, and `scope_rules` (including batch target creation).
3. **Auth Profiles**:
   - Create reusable credentials (`account_a`, `account_b`, `guest`, etc.) across **all 6 persisted `AuthType` modes**: `headers`, `bearer`, `cookie`, `basic`, `login_api`, `form_login` (plus optional Playwright `storage_state.json`).
   - Store secrets safely using `ENV:RECONQA_*` or `${RECONQA_*}` references—API responses mask raw credentials automatically.
   - Verify profile login configs live with **Test Login** (`POST /api/v1/projects/{project_id}/auth-profiles/{profile_id}/test-login`) or auto-extract session headers from any recorded HAR via **Extract from HAR** (`POST /api/v1/projects/{project_id}/auth-profiles/extract-from-har`).

### Business Capability Map (`5-Defense Matrix`) & Scenarios (`L0`→`L4`)

1. **Capabilities (`/p/:projectId/capabilities`)**:
   - `GET /api/v1/projects/{project_id}/capabilities` synthesizes hundreds of raw endpoints into `Resource × Action × Role × State` business capabilities across a **5-Defense Matrix**:
     1. Happy Path
     2. Invalid Params
     3. Illegal State
     4. AuthZ / Role
     5. Consistency / Idempotency
   - Toggle `Core Only` / `All Live` / `All`, and click **Promote 5-Case Scenario** (`POST /api/v1/projects/{project_id}/capabilities/promote-scenario`) to materialize a structured `Scenario` idempotently.
2. **Scenarios (`/p/:projectId/scenarios`)**:
   - Manage `P0`–`P3` scenarios (`GET/POST/PATCH/DELETE /api/v1/projects/{project_id}/scenarios`, `POST .../scenarios/sync`) and track maturity from `L0` to `L4`.
   - **Zero-JDK `L0`→`L4` Loop**: Run online **Chain Execute / Resume** or **TryIt Replay** with `sync_scenarios=True` to verify assertions and advance Scenario L3 status without needing `mvn test` or a JDK. When desired, you can also run external runners (`java_test`, `schemathesis`, `nuclei`, `k6`) via `POST /api/v1/projects/{project_id}/runs/{run_id}/execute-tool` (`sync_scenarios=True`).

---

## 5. Workflow D: AI Agent Collaboration (`reconqa mcp`)

ReconQA ships a thin **FastMCP Server** (`reconqa mcp`) exposing **22 tools and 3 resources** (`openapi`, `correlation`, `knowledge`), paired with `skills/reconqa/SKILL.md`:

1. Install the MCP extra and start the FastAPI backend:
   ```bash
   uv sync --extra dev --extra mcp
   make serve
   ```
2. Register `reconqa mcp` in your MCP client (`stdio`, `streamable-http`, or `sse` transport talking to `http://127.0.0.1:8000`).
3. Key agent workflows include `reconqa_list_auth_profiles`, `reconqa_explore_from_seed` (with `credential_id` / `auth`), `reconqa_get_capability_map`, `reconqa_promote_capability_scenario`, `reconqa_replay_endpoint(..., sync_scenarios=True)`, `reconqa_execute_chain(..., sync_scenarios=True)`, and `reconqa_resume_chain(..., sync_scenarios=True)`.

For full client configuration snippets, all 22 tool signatures, and playbooks, see **[AI Agent & MCP Integration](agent-integration.md)**.

---

## 6. Toolchain Doctor & Optional Tool Installation

ReconQA tracks 14 optional external tools across discovery, recording, Java execution, load testing, fuzzing, and security scanning.

- **Web Console**: Open **`/system/doctor`** (or the Overview **Toolchain Readiness** strip) to inspect availability, impact, install commands, Docker alternatives, or run one-click user-space installation (`POST /api/v1/system/doctor/install`).
- **CLI**: `uv run reconqa doctor` or `./scripts/install-tools.sh ./bin [--only <tool>]`.
- **Docker Alternatives**:
  - Java TestNG + Allure: `JAVA_TESTS_DIR=./workspaces/runs/<run_id>/java-tests docker compose --profile runner run --rm java-runner`
  - k6 Load Test: `docker run --rm -i grafana/k6 run - < workspaces/runs/<run_id>/performance/k6/scenario_ramp.js`
  - mitmproxy Recorder: `docker compose --profile recorder up -d mitmproxy`
  - OWASP ZAP: `docker compose --profile security up -d zap`

> **Binary Resolution Order**: `RECONQA_BIN_DIR` → `~/.reconqa/bin` → `./bin` → current Python environment → system `PATH`.

---

## 7. Workflow E: Deliverable Exports & Quality Gates (R4 & R5)

### Standalone Acceptance Report (`html` / `markdown` / `json`)

Generate a self-contained acceptance report aggregating API coverage, the 5-Defense Capability Map matrix, `P0`/`P1` `L0`–`L4` effectiveness statistics, chain assertion execution evidence, and High/Critical security & performance findings:

- **Web UI**: Click **Export HTML Report** or **Export Markdown Report** on **Project Overview (`/p/:projectId/overview`)** or **Capabilities (`/p/:projectId/capabilities`)**.
- **REST API**:
  ```bash
  curl -o acceptance-report.html "http://127.0.0.1:8000/api/v1/projects/<project_id>/acceptance-report?format=html"
  curl -o acceptance-report.md "http://127.0.0.1:8000/api/v1/projects/<project_id>/acceptance-report?format=markdown"
  curl "http://127.0.0.1:8000/api/v1/projects/<project_id>/acceptance-report?format=json"
  ```
  (Optional `&run_id=<run_id>` scopes findings and performance evidence to a specific run; standalone HTML inlines all CSS with zero external CDN dependencies.)

### Postman Collection v2.1 Export

Export the project's cumulative asset library (grouped by prefix) and correlation chains (including `pm.test` status/JSON checks, `pm.collectionVariables.set` extraction scripts, and redacted/templatized auth placeholders) as a standard Postman Collection v2.1 JSON file:

- **Web UI**: Click **Export Postman v2.1** on **Overview**, **Capabilities**, or **Inventory**.
- **REST API**:
  ```bash
  curl -o postman-collection.json "http://127.0.0.1:8000/api/v1/projects/<project_id>/export/postman"
  ```

### Release Quality Gate (`reconqa gate` CLI, API & Webhooks)

Enforce automated release gates across 4 rules:
1. **`max_p0_chain_failures`** (default `0`): Maximum allowed failed P0 chains/assertions.
2. **`max_new_high_security`** (default `0`): Maximum allowed new High/Critical security findings vs. baseline (or total High+ findings when `fail_on_high_without_baseline` is `True`).
3. **`max_coverage_drop_pct`** (default `5.0`): Maximum allowed API coverage percentage drop vs. baseline.
4. **`max_p95_ms`** (default `None`, disabled if unset): Maximum allowed P95 latency in milliseconds.

**1. Configure defaults in `reconqa.yaml`** (`uv run reconqa init`):
```yaml
gate:
  max_p0_chain_failures: 0
  max_new_high_security: 0
  max_coverage_drop_pct: 5.0
  max_p95_ms: null
  fail_on_high_without_baseline: false
  feishu_webhook: null
  dingtalk_webhook: null
  slack_webhook: null
  webhook_timeout_sec: 10.0
```

**2. Run via CLI (`reconqa gate`)** — supports three evaluation modes (specify exactly one of `--run-dir` or `--project-id`):
```bash
# Mode 1: Offline workspace evaluation (no running server required)
uv run reconqa gate \
  --run-dir ./workspaces/current_run \
  --baseline-dir ./workspaces/baseline_run \
  --chain-runs-dir ./workspaces/chain_runs \
  --max-p0-chain-failures 0 \
  --max-new-high-security 0 \
  --max-coverage-drop 5.0 \
  --max-p95-ms 800 \
  --json

# Mode 2: Local backend DB evaluation (queries SQLite/PostgreSQL directly)
uv run reconqa gate \
  --project-id <project_id> \
  --run-id <run_id> \
  --baseline-run-id <baseline_run_id> \
  --config reconqa.yaml

# Mode 3: Remote/Live API evaluation + Feishu / DingTalk / Slack webhook notifications
uv run reconqa gate \
  --project-id <project_id> \
  --api-url http://127.0.0.1:8000 \
  --feishu-webhook https://open.feishu.cn/open-apis/bot/v2/hook/... \
  --dingtalk-webhook https://oapi.dingtalk.com/robot/send?access_token=... \
  --slack-webhook https://hooks.slack.com/services/... \
  --webhook-timeout 10.0 \
  --no-notify-on-pass
```

**3. Exit Codes**:
- `0`: All enabled quality gate rules passed.
- `1`: One or more quality gate rules failed, the API returned HTTP `>= 400` / unreachable, or the project/run lookup failed.
- `2`: Invalid CLI arguments (e.g., specifying neither or both `--run-dir` and `--project-id`, `--api-url` without `--project-id`, or missing directory/config path).

**4. REST API & Web UI**:
- Call `POST /api/v1/projects/{project_id}/gate` directly, or trigger **Run Quality Gate** in the embedded **Quality Gate Card** on **Run Overview (`/p/:projectId/runs/:runId`)** and **Historical Trends (`/p/:projectId/trends`)**.
