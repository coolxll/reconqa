import { afterEach, describe, expect, it, vi } from "vitest";
import { executeChain, resumeChainRun } from "@/api/chains";
import { ApiError } from "@/api/client";
import { replayEndpoint } from "@/api/projects";
import {
  downloadAcceptanceReport,
  downloadPostmanCollection,
  evaluateProjectQualityGate,
  fetchAcceptanceReportBlob,
  fetchPostmanCollectionBlob,
  getAcceptanceReport,
  getAcceptanceReportUrl,
  getPostmanExportUrl,
} from "@/api/reports";

describe("R4 reports/postman API & R5 quality gate & R1 sync contracts", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("builds properly encoded Acceptance Report & Postman export URLs", () => {
    expect(getAcceptanceReportUrl("proj/1")).toBe(
      "/api/v1/projects/proj%2F1/acceptance-report?format=html"
    );
    expect(
      getAcceptanceReportUrl("proj_1", { format: "markdown", runId: " run-99 " })
    ).toBe("/api/v1/projects/proj_1/acceptance-report?format=markdown&run_id=run-99");
    expect(getAcceptanceReportUrl("proj_1", { format: "json", runId: "   " })).toBe(
      "/api/v1/projects/proj_1/acceptance-report?format=json"
    );

    expect(getPostmanExportUrl("proj/1")).toBe(
      "/api/v1/projects/proj%2F1/export/postman"
    );
    expect(getPostmanExportUrl("proj_1", "run-7")).toBe(
      "/api/v1/projects/proj_1/export/postman?run_id=run-7"
    );
  });

  it("fetches Acceptance Report JSON data with format=json", async () => {
    const mockReport = {
      project_id: "proj_1",
      project_name: "Demo",
      project_slug: "demo",
      run_id: "run_1",
      generated_at: "2026-11-25T00:00:00Z",
      summary: {
        overall_status: "passed",
        api_coverage_pct: 85.5,
        capability_coverage_pct: 90,
        p0_p1_effective_pct: 100,
        chain_pass_rate_pct: 100,
        high_critical_security_count: 0,
        high_critical_perf_count: 0,
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(mockReport), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const data = await getAcceptanceReport("proj_1", "run_1");
    expect(data.project_id).toBe("proj_1");
    expect(data.summary.overall_status).toBe("passed");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/projects/proj_1/acceptance-report?format=json&run_id=run_1"
    );
  });

  it("fetches Acceptance Report and Postman blobs with Content-Disposition filename and propagates ApiError", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response("<html><body>Report</body></html>", {
          status: 200,
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Disposition": 'attachment; filename="demo-acceptance-report.html"',
          },
        })
      )
      .mockResolvedValueOnce(
        new Response('{"info":{"name":"Demo"}}', {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            "Content-Disposition": 'attachment; filename="demo-postman-collection.json"',
          },
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Project not found" }), {
          status: 404,
          statusText: "Not Found",
          headers: { "Content-Type": "application/json" },
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    const htmlRes = await fetchAcceptanceReportBlob("proj_1", { format: "html" });
    expect(htmlRes.filename).toBe("demo-acceptance-report.html");
    expect(htmlRes.contentType).toContain("text/html");
    expect(await htmlRes.blob.text()).toContain("Report");

    const postmanName = await downloadPostmanCollection("proj_1", "run-2");
    expect(postmanName).toBe("demo-postman-collection.json");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/projects/proj_1/export/postman?run_id=run-2"
    );

    await expect(downloadAcceptanceReport("missing", { format: "markdown" })).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      detail: "Project not found",
    });
  });

  it("extracts fallback filename on downloadAcceptanceReport when Content-Disposition is absent", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response("# Acceptance Report", {
          status: 200,
          headers: { "Content-Type": "text/markdown; charset=utf-8" },
        })
      )
      .mockResolvedValueOnce(
        new Response('{"item":[]}', {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    const resolved = await downloadAcceptanceReport("proj_x", { format: "markdown" });
    expect(resolved).toBe("proj_x-acceptance-report.md");

    const blobRes = await fetchPostmanCollectionBlob("proj_x");
    expect(blobRes.filename).toBeNull();
  });

  it("posts Quality Gate evaluation with default empty body and threshold overrides, and surfaces ApiError", async () => {
    const gateResp = {
      project_id: "proj_1",
      project_name: "Demo",
      target_label: "run_2",
      run_id: "run_2",
      baseline_run_id: "run_1",
      passed: false,
      status: "failed",
      evaluated_at: "2026-11-25T01:00:00Z",
      rules: [
        {
          rule: "p0_chain_failures",
          label: "P0 Chain Failures",
          passed: false,
          skipped: false,
          actual: 1,
          threshold: 0,
          message: "1 P0 chain failure(s) exceeded limit 0",
          details: {},
        },
      ],
      metrics: {
        p0_chain_failures: 1,
        p0_failed_chains: 1,
        p0_failed_assertions: 2,
        total_chain_runs: 3,
        high_security_count: 0,
        baseline_high_security_count: 0,
        new_high_security_count: 0,
        current_coverage_pct: 82.0,
        baseline_coverage_pct: 80.0,
        coverage_drop_pct: -2.0,
        p95_ms: 120,
        p95_source: "har",
        baseline_available: true,
      },
      summary: "Quality gate FAILED",
      webhook_results: [],
    };

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(gateResp), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(gateResp), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Baseline run not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    const resDefault = await evaluateProjectQualityGate("proj_1");
    expect(resDefault.passed).toBe(false);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/projects/proj_1/gate");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[0][1]?.body).toBe("{}");
    const hdrs0 = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(hdrs0.get("Content-Type")).toBe("application/json");

    await evaluateProjectQualityGate("proj_1", {
      run_id: "run_2",
      baseline_run_id: "run_1",
      max_p0_chain_failures: 0,
      max_coverage_drop_pct: 5.0,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      run_id: "run_2",
      baseline_run_id: "run_1",
      max_p0_chain_failures: 0,
      max_coverage_drop_pct: 5.0,
    });

    await expect(
      evaluateProjectQualityGate("proj_1", { baseline_run_id: "bad" })
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("serializes R1 sync_scenarios and include_scenario_assertions across replay, chain execute, and resume", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status_code: 200,
            status_text: "OK",
            duration_ms: 42,
            response_headers: {},
            body_text: '{"ok":true}',
            content_type: "application/json",
            truncated: false,
            assertion_results: [],
            synced_scenarios: 1,
            synced_assertions: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            chain_run_id: "cr_1",
            project_id: "proj_1",
            chain_name: "checkout",
            status: "passed",
            total_steps: 2,
            completed_steps: 2,
            base_url: "https://api.example.com",
            context_vars: {},
            step_results: [],
            synced_scenarios: 2,
            synced_assertions: 4,
            created_at: "2026-11-25T00:00:00Z",
            updated_at: "2026-11-25T00:00:01Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            chain_run_id: "cr_1",
            project_id: "proj_1",
            chain_name: "checkout",
            status: "passed",
            total_steps: 2,
            completed_steps: 2,
            base_url: "https://api.example.com",
            context_vars: {},
            step_results: [],
            synced_scenarios: 1,
            synced_assertions: 1,
            created_at: "2026-11-25T00:00:00Z",
            updated_at: "2026-11-25T00:00:02Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      );
    vi.stubGlobal("fetch", fetchMock);

    const replayRes = await replayEndpoint("proj_1", {
      method: "POST",
      url: "https://api.example.com/orders",
      include_scenario_assertions: false,
      sync_scenarios: false,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
      include_scenario_assertions: false,
      sync_scenarios: false,
    });
    expect(replayRes.synced_scenarios).toBe(1);
    expect(replayRes.synced_assertions).toBe(2);

    const execRes = await executeChain("proj_1", {
      run_id: "run_1",
      chain_name: "checkout",
      sync_scenarios: true,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({
      sync_scenarios: true,
    });
    expect(execRes.synced_scenarios).toBe(2);
    expect(execRes.synced_assertions).toBe(4);

    const resumeRes = await resumeChainRun("proj_1", "cr_1", {
      from_step_index: 1,
      sync_scenarios: true,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toMatchObject({
      from_step_index: 1,
      sync_scenarios: true,
    });
    expect(resumeRes.synced_scenarios).toBe(1);
    expect(resumeRes.synced_assertions).toBe(1);
  });
});
