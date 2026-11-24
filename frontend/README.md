# ReconQA Frontend (Phase 2 — Vue 3 + Vite)

This directory reserves the Vue 3 + Vite web console skeleton for Phase 2.

## API Contract (Backend Ready in Phase 1)

- **Dev Proxy**: `vite.config.ts` proxies `/api` and `/healthz` to `http://localhost:8000`.
- **CORS**: Backend enables `http://localhost:5173` and `http://127.0.0.1:5173` by default (`RECONQA_CORS_ORIGINS`).
- **Base Prefix**: `/api/v1`
- **Key Endpoints**:
  - Projects / Targets / Auth Profiles:
    - `GET/POST /api/v1/projects`
    - `GET/POST /api/v1/projects/{project_id}/targets`
    - `GET/POST /api/v1/projects/{project_id}/auth-profiles`
    - `POST /api/v1/projects/{project_id}/auth-profiles/{profile_id}/storage-state`
  - Runs, HAR Upload/Reuse & Live SSE:
    - `POST /api/v1/projects/{project_id}/runs?auto_start=false`
    - `POST /api/v1/runs/{run_id}/hars` (multipart upload)
    - `POST /api/v1/runs/{run_id}/hars/reuse`
    - `POST /api/v1/runs/{run_id}/start`
    - `GET /api/v1/runs/{run_id}`
    - `GET /api/v1/runs/{run_id}/events` (`EventSource` SSE stream: `snapshot`, `run_started`, `job_running`, `job_completed`, `log`, `run_completed`, `run_failed`)
  - Artifacts & Reports:
    - `GET /api/v1/runs/{run_id}/openapi?format=json|yaml`
    - `GET /api/v1/runs/{run_id}/correlation` (JSON + Mermaid graph)
    - `GET /api/v1/runs/{run_id}/coverage`
    - `GET /api/v1/runs/{run_id}/artifacts` & `GET /api/v1/runs/{run_id}/artifacts/{kind}`
    - `POST /api/v1/runs/{run_id}/burp-export`
    - `GET /api/v1/runs/{run_id}/findings?category=&severity=`
    - `GET /api/v1/runs/{run_id}/security` & `GET /api/v1/runs/{run_id}/performance`
    - `GET /api/v1/projects/{project_id}/trends`

## Planned Local Dev Flow (Phase 2)

```bash
cd frontend
npm install
npm run dev
```
