import { describe, expect, it } from "vitest";
import type { ScenarioAssertion, ScenarioAssertionOperator } from "@/api/types";
import {
  ALL_ASSERTION_OPERATORS,
  ASSERTION_OPERATOR_GROUPS,
  computeResumeStepIndex,
  extractTemplateVars,
  filterScenarioAssertionsForEndpoint,
  getOperatorMeta,
  isSqlOperator,
  matchesChainCheckpoint,
} from "@/utils/assertionOperators";

const EXPECTED_BACKEND_OPERATORS: ScenarioAssertionOperator[] = [
  "equals",
  "not_equals",
  "not_empty",
  "is_empty",
  "gt",
  "gte",
  "lt",
  "lte",
  "contains",
  "not_contains",
  "matches",
  "array_contains",
  "length_eq",
  "length_gt",
  "type_is",
  "status_code",
  "header_equals",
  "latency_lt",
  "json_schema",
  "var_equals",
  "sql_equals",
  "sql_exists",
  "custom",
];

describe("assertionOperators metadata & helpers", () => {
  it("contains all 23 backend-contract operators without duplicates", () => {
    expect(ALL_ASSERTION_OPERATORS).toHaveLength(23);
    expect(new Set(ALL_ASSERTION_OPERATORS).size).toBe(23);
    expect([...ALL_ASSERTION_OPERATORS].sort()).toEqual(
      [...EXPECTED_BACKEND_OPERATORS].sort()
    );
  });

  it("organizes all 23 operators into the 5 expected categories", () => {
    expect(ASSERTION_OPERATOR_GROUPS.map((g) => g.id)).toEqual([
      "basic",
      "numeric_collection",
      "contract_cross_step",
      "database_readonly",
      "custom",
    ]);

    const flattened = ASSERTION_OPERATOR_GROUPS.flatMap((g) =>
      g.operators.map((o) => o.value)
    );
    expect(flattened).toHaveLength(23);
    expect(new Set(flattened).size).toBe(23);

    for (const group of ASSERTION_OPERATOR_GROUPS) {
      expect(group.labelZh.trim().length).toBeGreaterThan(0);
      expect(group.labelEn.trim().length).toBeGreaterThan(0);
      for (const op of group.operators) {
        expect(op.category).toBe(group.id);
        expect(op.labelZh.trim().length).toBeGreaterThan(0);
        expect(op.labelEn.trim().length).toBeGreaterThan(0);
        expect(op.jsonpathLabelZh.trim().length).toBeGreaterThan(0);
        expect(op.jsonpathLabelEn.trim().length).toBeGreaterThan(0);
        expect(op.jsonpathPlaceholder.trim().length).toBeGreaterThan(0);
        expect(op.expectedPlaceholder.trim().length).toBeGreaterThan(0);
      }
    }
  });

  it("detects SQL operators accurately and flags needsDbDsn only for SQL operators", () => {
    expect(isSqlOperator("sql_equals")).toBe(true);
    expect(isSqlOperator("sql_exists")).toBe(true);
    expect(isSqlOperator("equals")).toBe(false);
    expect(isSqlOperator("var_equals")).toBe(false);
    expect(isSqlOperator("unknown_op")).toBe(false);

    for (const op of ALL_ASSERTION_OPERATORS) {
      const meta = getOperatorMeta(op);
      expect(meta.needsDbDsn).toBe(isSqlOperator(op));
    }
  });

  it("returns tailored placeholders for specialized operators and falls back safely", () => {
    expect(getOperatorMeta("sql_equals").jsonpathPlaceholder).toContain("SELECT");
    expect(getOperatorMeta("sql_exists").needsExpected).toBe(false);
    expect(getOperatorMeta("not_empty").needsExpected).toBe(false);
    expect(getOperatorMeta("is_empty").needsExpected).toBe(false);
    expect(getOperatorMeta("latency_lt").expectedPlaceholder).toBe("500");
    expect(getOperatorMeta("var_equals").expectedPlaceholder).toContain("{{before_balance}}");
    expect(getOperatorMeta("header_equals").jsonpathPlaceholder).toBe("Content-Type");

    // Unknown operator falls back safely to custom
    const fallback = getOperatorMeta("nonexistent_operator");
    expect(fallback.value).toBe("custom");
  });

  it("computes checkpoint resume step index across normal and edge cases", () => {
    expect(computeResumeStepIndex(null)).toBe(0);
    expect(computeResumeStepIndex(undefined)).toBe(0);
    expect(computeResumeStepIndex({ failed_step_index: null, completed_steps: 0, total_steps: 0 })).toBe(0);

    // Failed at step index 2 (Step 3)
    expect(
      computeResumeStepIndex({ failed_step_index: 2, completed_steps: 2, total_steps: 5 })
    ).toBe(2);

    // Clamps out-of-bounds failed_step_index
    expect(
      computeResumeStepIndex({ failed_step_index: 99, completed_steps: 2, total_steps: 4 })
    ).toBe(3);
    expect(
      computeResumeStepIndex({ failed_step_index: -3, completed_steps: 1, total_steps: 4 })
    ).toBe(0);

    // Partial progress without explicit failed_step_index
    expect(
      computeResumeStepIndex({ failed_step_index: null, completed_steps: 2, total_steps: 5 })
    ).toBe(2);

    // Fully passed run restarts from step 0
    expect(
      computeResumeStepIndex({ failed_step_index: null, completed_steps: 5, total_steps: 5 })
    ).toBe(0);
  });

  it("extracts unique {{template}} variable names in order from strings and nested objects", () => {
    expect(
      extractTemplateVars("SELECT status FROM orders WHERE id = {{order_id}} AND user = {{ user_id }}")
    ).toEqual(["order_id", "user_id"]);

    expect(
      extractTemplateVars("{{before_balance}} - 50 == {{after_balance}} + {{before_balance}}")
    ).toEqual(["before_balance", "after_balance"]);

    expect(
      extractTemplateVars({
        path: "/api/orders/{{order.id}}/items/{{item-id}}",
        body: [{ token: "{{auth_token}}" }, { repeat: "{{order.id}}" }],
      })
    ).toEqual(["order.id", "item-id", "auth_token"]);

    expect(extractTemplateVars("no variables here")).toEqual([]);
    expect(extractTemplateVars(null)).toEqual([]);
  });

  it("matches checkpoints by chain_index when present and falls back to chain_name", () => {
    // Two chains sharing the same name must not collide when chain_index is set
    expect(
      matchesChainCheckpoint({ chain_index: 0, chain_name: "checkout_flow" }, 0, "checkout_flow")
    ).toBe(true);
    expect(
      matchesChainCheckpoint({ chain_index: 0, chain_name: "checkout_flow" }, 1, "checkout_flow")
    ).toBe(false);
    expect(
      matchesChainCheckpoint({ chain_index: 2, chain_name: "renamed_flow" }, 2, "checkout_flow")
    ).toBe(true);

    // Legacy checkpoints without chain_index fall back to chain_name
    expect(
      matchesChainCheckpoint({ chain_index: null, chain_name: "checkout_flow" }, 1, "checkout_flow")
    ).toBe(true);
    expect(
      matchesChainCheckpoint({ chain_index: undefined, chain_name: "other_flow" }, 1, "checkout_flow")
    ).toBe(false);
  });

  it("filters scenario assertions matching a specific endpoint or wildcard target", () => {
    const sample: ScenarioAssertion[] = [
      {
        id: "1",
        title: "Order ID present",
        target_endpoint: "POST /api/v1/orders",
        jsonpath: "$.data.id",
        operator: "not_empty",
        expected: "",
        note: "",
        status: "pending",
      },
      {
        id: "2",
        title: "Global latency budget",
        target_endpoint: "",
        jsonpath: "",
        operator: "latency_lt",
        expected: "500",
        note: "",
        status: "pending",
      },
      {
        id: "3",
        title: "Bare path match",
        target_endpoint: "/api/v1/orders",
        jsonpath: "$.code",
        operator: "equals",
        expected: "0",
        note: "",
        status: "pending",
      },
      {
        id: "4",
        title: "Other endpoint",
        target_endpoint: "GET /api/v1/orders",
        jsonpath: "$.data.items",
        operator: "length_gt",
        expected: "0",
        note: "",
        status: "pending",
      },
    ];

    const matched = filterScenarioAssertionsForEndpoint(sample, "post", "/api/v1/orders/?dry=1");
    expect(matched.map((a) => a.id)).toEqual(["1", "2", "3"]);

    const getMatched = filterScenarioAssertionsForEndpoint(sample, "GET", "/api/v1/orders");
    expect(getMatched.map((a) => a.id)).toEqual(["2", "3", "4"]);
  });
});
