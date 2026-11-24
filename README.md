# ReconQA

**One-recording, multi-use Web/API testing platform** powered by **HAR 1.2 + OpenAPI 3.0.3**.

ReconQA takes a single round of discovery and scenario recording (or existing HAR files from Chrome DevTools, Charles, Fiddler, Playwright, or mitmproxy) and automatically produces:

1. **Unified API Contract & Data-Flow Hub**: Standard OpenAPI 3.0.3 (`openapi.yaml` / `openapi.json`), data-flow correlation graph (`correlation.json` / `correlation.mmd`), and live-vs-static JS coverage diff (`coverage_diff.json` / `coverage_diff.md`).
2. **Java API Automation Project**: Ready-to-run Maven project (`TestNG 7.10` + `RestAssured 5.5` + `Allure 2.29` + `JSON Schema Draft-07`), structured into L1 CSV-driven contract smoke tests, L2 stateful correlation chains with `@AfterClass` teardown, and L3 auth boundary checks.
3. **Security Audit & Dual-Account IDOR Matrix**: Static security header / cookie / redacted leak analysis, unauthenticated replay and read-only-by-default dual-account A/B IDOR checks, optional Nuclei & OWASP ZAP integration, and Burp Repeater `.http` + cURL exports.
4. **Performance Profiling & k6 Load Testing**: HAR bottleneck profiling (TTFB, payload size, missing compression, duplicate requests, CDN/cache hit rate), parameterized ramping `k6` scripts (`NO_CONN_REUSE`, proxy egress, `429`/`5xx`/`p95` thresholds), and an optional local round-robin proxy pool.

---

## Architecture

```text
Discovery (PD tools + SPA parser)
        │
        ▼
Scenario Recorder (Playwright interactive/auto, mitmproxy) OR Import Existing HAR
        │
        ▼
Unified Data Hub (HAR 1.2 → OpenAPI 3.0.3 + Correlation + JS Coverage Diff)
        ├──► Java Automation (Maven + TestNG + RestAssured + Allure + Schemathesis)
        ├──► Security Audit (Static Headers/Cookies/Leaks + A/B IDOR + Burp + Nuclei)
        └──► Performance (HAR Profiler + Parameterized k6 Ramp + Proxy Pool)
```

---

## Quick Start

### 1. Install Dependencies

```bash
# Using uv (recommended)
uv sync --extra dev

# Optional extras for browser/proxy recording or fuzzing
uv sync --extra recorder --extra fuzz
```

### 2. Install External CLI Tools (Optional)

```bash
./scripts/install-tools.sh ./bin
uv run reconqa doctor
```

### 3. Run Pipeline from Existing HAR Files

```bash
# Initialize project config
uv run reconqa init -o reconqa.yaml

# Run end-to-end pipeline reusing existing HARs
uv run reconqa pipeline \
  --target-url https://api.example.com \
  --har ./sample_hars/ \
  -o ./workspaces/demo_run
```

### 4. Interactive Scenario Recording

```bash
# Headed Playwright interactive multi-scenario recording
uv run reconqa record https://app.example.com \
  --interactive \
  --save-storage-state auth_a.json \
  -o ./workspaces/demo_run/har

# Or proxy recording with mitmproxy (switch scenarios via http://reconqa.local/scenario/<name>)
uv run reconqa record --mitm --port 8080 -o ./workspaces/demo_run/har
```

### 5. Run Generated Java Tests

With local Maven or via Docker (`docker/Dockerfile.java-runner`):

```bash
docker run --rm --network host \
  -v "$(pwd)/workspaces/demo_run/java-tests:/work" -w /work \
  -e RECONQA_BASE_URI=https://api.example.com \
  -e RECONQA_AUTH_TOKEN=your-test-token \
  maven:3.9-eclipse-temurin-21 mvn test
```

### 6. Run Generated k6 Load Test

```bash
k6 run \
  -e BASE_URL=https://api.example.com \
  -e AUTH_TOKEN=your-test-token \
  -e NO_CONN_REUSE=false \
  ./workspaces/demo_run/performance/k6/scenario_ramp.js
```

---

## FastAPI Server & SSE API

```bash
uv run reconqa serve --host 0.0.0.0 --port 8000 --reload
```

- Swagger UI: `http://localhost:8000/docs`
- Health check: `GET /healthz`
- REST + SSE prefix: `/api/v1`
  - `POST /api/v1/projects`, `/projects/{id}/targets`, `/projects/{id}/auth-profiles`
  - `POST /api/v1/projects/{id}/runs?auto_start=false`
  - `POST /api/v1/runs/{run_id}/hars` (upload `.har` files) & `POST /api/v1/runs/{run_id}/hars/reuse`
  - `POST /api/v1/runs/{run_id}/start` & `GET /api/v1/runs/{run_id}/events` (SSE stream)
  - `GET /api/v1/runs/{run_id}/openapi?format=yaml|json`, `/correlation`, `/coverage`
  - `GET /api/v1/runs/{run_id}/artifacts/{kind}` (downloads file or zips `java-tests` / `burp_exports`)
  - `GET /api/v1/runs/{run_id}/findings`, `/security`, `/performance`, `/projects/{id}/trends`

---

## Docker Compose

```bash
# Build and launch backend API
docker compose up -d --build api

# Run containerized Java + Allure test runner
JAVA_TESTS_DIR=./workspaces/demo_run/java-tests docker compose --profile runner run --rm java-runner

# Optional profiles: recorder (mitmproxy), security (OWASP ZAP), prod (PostgreSQL)
```

---

## Development

```bash
make dev-install
make lint
make test
```

## License

MIT — see [LICENSE](LICENSE).
