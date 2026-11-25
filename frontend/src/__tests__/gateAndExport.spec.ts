import { describe, expect, it } from "vitest";
import {
  buildQualityGateRequest,
  formatGateMetricValue,
  getGateRuleState,
  hasSyncFeedback,
  parseContentDispositionFilename,
  triggerBlobDownload,
} from "@/utils/gateAndExport";

describe("gateAndExport pure helpers", () => {
  it("parses Content-Disposition filenames across quoted, unquoted, RFC 5987, and rejects traversal", () => {
    expect(
      parseContentDispositionFilename('attachment; filename="demo-report.html"')
    ).toBe("demo-report.html");
    expect(
      parseContentDispositionFilename("attachment; filename=demo-report.md")
    ).toBe("demo-report.md");
    expect(
      parseContentDispositionFilename(
        "attachment; filename=\"fallback.json\"; filename*=UTF-8''orders%20postman%20v2.1.json"
      )
    ).toBe("orders postman v2.1.json");

    expect(parseContentDispositionFilename(null)).toBeNull();
    expect(parseContentDispositionFilename("")).toBeNull();
    expect(parseContentDispositionFilename("attachment; filename=\"../evil.html\"")).toBeNull();
    expect(parseContentDispositionFilename("attachment; filename=\"sub/evil.html\"")).toBeNull();
    expect(parseContentDispositionFilename("attachment; filename=\"sub\\evil.html\"")).toBeNull();
    expect(parseContentDispositionFilename("attachment; filename=\"..\"")).toBeNull();
  });

  it("builds QualityGateRequest safely, preserving explicit 0 and never attaching webhook keys", () => {
    const emptyReq = buildQualityGateRequest({
      runId: "  ",
      baselineRunId: "",
      maxP0ChainFailures: "",
      maxNewHighSecurity: null,
      maxCoverageDropPct: undefined,
      maxP95Ms: "not-a-number",
    });
    expect(emptyReq).toEqual({});
    expect("feishu_webhook" in emptyReq).toBe(false);
    expect("dingtalk_webhook" in emptyReq).toBe(false);
    expect("slack_webhook" in emptyReq).toBe(false);

    const zeroReq = buildQualityGateRequest({
      runId: " run-2 ",
      baselineRunId: " run-1 ",
      maxP0ChainFailures: "0",
      maxNewHighSecurity: 0,
      maxCoverageDropPct: "0",
      maxP95Ms: 250,
      failOnHighWithoutBaseline: false,
    });
    expect(zeroReq).toEqual({
      run_id: "run-2",
      baseline_run_id: "run-1",
      max_p0_chain_failures: 0,
      max_new_high_security: 0,
      max_coverage_drop_pct: 0,
      max_p95_ms: 250,
      fail_on_high_without_baseline: false,
    });

    // Negative numbers are ignored rather than emitting invalid negative thresholds
    const negReq = buildQualityGateRequest({
      maxP0ChainFailures: -1,
      maxCoverageDropPct: "-2.5",
    });
    expect(negReq).toEqual({});
  });

  it("derives gate rule state and formats metric values consistently", () => {
    expect(getGateRuleState({ passed: true, skipped: false })).toBe("passed");
    expect(getGateRuleState({ passed: false, skipped: false })).toBe("failed");
    expect(getGateRuleState({ passed: true, skipped: true })).toBe("skipped");
    expect(getGateRuleState({ passed: false, skipped: true })).toBe("skipped");

    expect(formatGateMetricValue({ rule: "p0_chain_failures" }, 0)).toBe("0");
    expect(formatGateMetricValue({ rule: "new_high_security" }, 3)).toBe("3");
    expect(formatGateMetricValue({ rule: "coverage_drop_pct" }, 5.25)).toBe("5.25%");
    expect(formatGateMetricValue({ rule: "p95_latency_ms" }, 412.6)).toBe("413 ms");
    expect(formatGateMetricValue({ rule: "p95_latency_ms" }, null)).toBe("—");
    expect(formatGateMetricValue({ rule: "coverage_drop_pct" }, undefined)).toBe("—");
  });

  it("detects R1 sync feedback counts cleanly and no-ops triggerBlobDownload outside DOM", () => {
    expect(hasSyncFeedback(null)).toBe(false);
    expect(hasSyncFeedback({})).toBe(false);
    expect(hasSyncFeedback({ synced_scenarios: 0, synced_assertions: 0 })).toBe(false);
    expect(hasSyncFeedback({ synced_scenarios: 1, synced_assertions: 0 })).toBe(true);
    expect(hasSyncFeedback({ synced_scenarios: 0, synced_assertions: 3 })).toBe(true);

    // Must not throw in Node Vitest environment without DOM
    expect(() =>
      triggerBlobDownload(new Blob(["test"], { type: "text/plain" }), "test.txt")
    ).not.toThrow();
  });
});
