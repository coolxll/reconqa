# ReconQA Web Console (Vue 3 + Vite + TypeScript)

The interactive bilingual (**ZH / EN**) web console for ReconQA, built with **Vue 3**, **Vite**, **TypeScript**, **Tailwind CSS**, **ECharts**, **Mermaid**, and **Lucide Icons**.

---

## Tech Stack

- **Framework**: Vue `3.5` (`<script setup lang="ts">`) + Vue Router `4.4`
- **Build & Typecheck**: Vite `5.4` + TypeScript `5.6` + `vue-tsc`
- **Styling & Icons**: Tailwind CSS `3.4` + `lucide-vue-next`
- **Visualizations**: ECharts `5.5` (performance & trend charts) + Mermaid `11.3` (data-flow correlation graphs)
- **Internationalization**: Lightweight built-in ZH / EN store (`src/i18n/index.ts`, auto-detects browser locale and persists to `localStorage` under `reconqa_locale`)

---

## Views & Routes (`src/router/index.ts`)

| Route | View | Description |
| :--- | :--- | :--- |
| `/` | `OverviewView.vue` | Workflow hero banner, **isolated per-project status card grid** (latest run status, endpoints, coverage, high/critical findings, quick actions), recent runs table, and actionable **Toolchain Doctor** panel with one-click install modal (`POST /api/v1/system/doctor/install`). |
| `/projects` | `ProjectsView.vue` | **Project Knowledge Base** (`knowledge_docs`: inline Markdown, external links, file upload/download, category & search filters), **Targets**, and **Auth Profiles** (`account_a` / `account_b` / `guest` + `storage_state.json` upload). |
| `/explore` | `ExploreView.vue` | Decoupled **Explore & Discovery** workspace: paste 1 seed cURL or URL, probe Swagger/OpenAPI & HTML/Webpack/Vite JS bundles + sibling API hosts, inspect discovered endpoints/hosts/SPA routes, and **Promote to Test Run**. |
| `/runs` | `RunsView.vue` | Historical run list and **2-step zero-friction New Analysis modal** (`/runs?new=1`): Step 1 (Upload `.har`, Paste DevTools cURL/fetch/Raw HTTP, or Reuse HARs) → Step 2 (Auto-detect/create/select project) → runs fast core Hub analysis (`["merge"]`) first. |
| `/runs/:runId` | `RunDetailView.vue` | 7-tab Run Inspector: **Overview & Live SSE Terminal**, **OpenAPI 3.0.3 Explorer**, **Data-Flow Correlation (Mermaid) & JS Coverage**, **API Automation (Java TestNG + Allure)**, **Performance (HAR Profiler + k6)**, **Security (Static Audit + Auth/IDOR + Burp Drawer)**, and **Artifacts**. Capability tabs support progressive on-demand execution (`POST /api/v1/runs/{runId}/execute-modules`). |
| `/trends` | `TrendsView.vue` | Multi-run ECharts coverage growth, severity distribution, latency trends, and run-to-run diff comparison. |

---

## Source Structure

```text
frontend/
├── index.html
├── package.json
├── vite.config.ts                # Proxies /api and /healthz -> VITE_API_PROXY (default http://127.0.0.1:8000)
├── tailwind.config.js
└── src/
    ├── main.ts
    ├── App.vue
    ├── router/index.ts           # 6 application routes
    ├── i18n/
    │   ├── index.ts              # Reactive ZH/EN locale store
    │   └── locales/              # common.ts, explore.ts, runs.ts, dashboards.ts
    ├── api/
    │   ├── client.ts             # Fetch wrapper & error normalization
    │   ├── types.ts              # Shared TypeScript interfaces matching FastAPI schemas
    │   ├── projects.ts           # Projects, Targets, Auth Profiles, Knowledge Base API
    │   ├── explore.ts            # Explore sessions & Promote-to-Run API
    │   ├── runs.ts               # Runs, HAR upload/reuse, import-text, execute-modules, SSE
    │   └── reports.ts            # Doctor, Java/Allure, Security, Performance, Findings, Trends
    ├── views/                    # 6 route views
    └── components/
        ├── layout/               # AppShell, SidebarNav, TopBar, MetricCard, StatusBadge, EmptyState
        ├── runs/                 # HarUploadModal, SseLogConsole, OpenApiExplorer, CorrelationPanel, ArtifactsPanel
        └── dashboards/           # JavaAllureBoard, PerformanceBoard, SecurityBoard, BurpExportDrawer
```

---

## Local Development & Build

Ensure the FastAPI backend is running on port `8000` (`make serve` from the repository root), then start Vite:

```bash
cd frontend
npm install
npm run dev          # Starts Vite on http://localhost:5173 (proxies /api & /healthz -> http://127.0.0.1:8000)
```

Override the backend proxy target if needed:

```bash
VITE_API_PROXY=http://127.0.0.1:8080 npm run dev
```

Type-check and build the production bundle (`frontend/dist/`):

```bash
npm run typecheck    # vue-tsc --noEmit
npm run build        # vue-tsc --noEmit && vite build
npm run preview      # Preview production bundle on http://localhost:4173
```

---

## Production Container

`docker/Dockerfile.frontend` builds the static SPA using Node 22 and serves it via Nginx (`docker/nginx.conf`) with SPA history fallback and SSE-safe reverse proxying (`proxy_buffering off`) to the `api` service:

```bash
docker compose up -d --build api frontend
# Console available at http://localhost:5173
```
