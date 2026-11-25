import type { ChainRunOut, ScenarioAssertion, ScenarioAssertionOperator } from "@/api/types";

export type AssertionOperatorCategory =
  | "basic"
  | "numeric_collection"
  | "contract_cross_step"
  | "database_readonly"
  | "custom";

export interface AssertionOperatorMeta {
  value: ScenarioAssertionOperator;
  category: AssertionOperatorCategory;
  labelZh: string;
  labelEn: string;
  jsonpathLabelZh: string;
  jsonpathLabelEn: string;
  jsonpathPlaceholder: string;
  expectedPlaceholder: string;
  needsDbDsn: boolean;
  needsExpected: boolean;
  needsJsonpath: boolean;
  hintZh?: string;
  hintEn?: string;
}

export interface AssertionOperatorGroup {
  id: AssertionOperatorCategory;
  labelZh: string;
  labelEn: string;
  operators: AssertionOperatorMeta[];
}

export const ASSERTION_OPERATOR_GROUPS: AssertionOperatorGroup[] = [
  {
    id: "basic",
    labelZh: "基础值与文本断言",
    labelEn: "Basic & String",
    operators: [
      {
        value: "equals",
        category: "basic",
        labelZh: "等于 (==)",
        labelEn: "Equals (==)",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.status",
        expectedPlaceholder: "SUCCESS",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "not_equals",
        category: "basic",
        labelZh: "不等于 (!=)",
        labelEn: "Not Equals (!=)",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.status",
        expectedPlaceholder: "FAILED",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "not_empty",
        category: "basic",
        labelZh: "非空 (not_empty)",
        labelEn: "Not Empty",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.orderId",
        expectedPlaceholder: "(无需填写期望值)",
        needsDbDsn: false,
        needsExpected: false,
        needsJsonpath: true,
      },
      {
        value: "is_empty",
        category: "basic",
        labelZh: "为空 (is_empty)",
        labelEn: "Is Empty",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.errors",
        expectedPlaceholder: "(无需填写期望值)",
        needsDbDsn: false,
        needsExpected: false,
        needsJsonpath: true,
      },
      {
        value: "contains",
        category: "basic",
        labelZh: "包含子串 (contains)",
        labelEn: "Contains Substring",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.message",
        expectedPlaceholder: "created",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "not_contains",
        category: "basic",
        labelZh: "不包含子串 (not_contains)",
        labelEn: "Does Not Contain",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.message",
        expectedPlaceholder: "exception",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "matches",
        category: "basic",
        labelZh: "正则匹配 (regex)",
        labelEn: "Matches Regex",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.orderNo",
        expectedPlaceholder: "^ORD-\\d{8,}$",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
    ],
  },
  {
    id: "numeric_collection",
    labelZh: "数值、集合与类型",
    labelEn: "Numeric, Collection & Type",
    operators: [
      {
        value: "gt",
        category: "numeric_collection",
        labelZh: "大于 (>)",
        labelEn: "Greater Than (>)",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.totalAmount",
        expectedPlaceholder: "0",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "gte",
        category: "numeric_collection",
        labelZh: "大于等于 (>=)",
        labelEn: "Greater or Equal (>=)",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.count",
        expectedPlaceholder: "1",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "lt",
        category: "numeric_collection",
        labelZh: "小于 (<)",
        labelEn: "Less Than (<)",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.discountRate",
        expectedPlaceholder: "1.0",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "lte",
        category: "numeric_collection",
        labelZh: "小于等于 (<=)",
        labelEn: "Less or Equal (<=)",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.errorCount",
        expectedPlaceholder: "0",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "array_contains",
        category: "numeric_collection",
        labelZh: "数组包含元素 (array_contains)",
        labelEn: "Array Contains Item",
        jsonpathLabelZh: "数组 JSONPath",
        jsonpathLabelEn: "Array JSONPath",
        jsonpathPlaceholder: "$.data.items[*].sku",
        expectedPlaceholder: "SKU-1001",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "length_eq",
        category: "numeric_collection",
        labelZh: "长度等于 (length_eq)",
        labelEn: "Length Equals",
        jsonpathLabelZh: "数组 / 字符串 JSONPath",
        jsonpathLabelEn: "Array / String JSONPath",
        jsonpathPlaceholder: "$.data.items",
        expectedPlaceholder: "3",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "length_gt",
        category: "numeric_collection",
        labelZh: "长度大于 (length_gt)",
        labelEn: "Length Greater Than",
        jsonpathLabelZh: "数组 / 字符串 JSONPath",
        jsonpathLabelEn: "Array / String JSONPath",
        jsonpathPlaceholder: "$.data.items",
        expectedPlaceholder: "0",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "type_is",
        category: "numeric_collection",
        labelZh: "数据类型匹配 (type_is)",
        labelEn: "Value Type Is",
        jsonpathLabelZh: "JSONPath 路径",
        jsonpathLabelEn: "JSONPath",
        jsonpathPlaceholder: "$.data.items",
        expectedPlaceholder: "array",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
        hintZh: "支持类型：string / number / boolean / array / object / null",
        hintEn: "Supported types: string / number / boolean / array / object / null",
      },
    ],
  },
  {
    id: "contract_cross_step",
    labelZh: "HTTP 契约与跨步骤变量",
    labelEn: "HTTP Contract & Cross-Step Vars",
    operators: [
      {
        value: "status_code",
        category: "contract_cross_step",
        labelZh: "HTTP 状态码 (status_code)",
        labelEn: "HTTP Status Code",
        jsonpathLabelZh: "路径（可选，留空验整条响应）",
        jsonpathLabelEn: "Path (optional)",
        jsonpathPlaceholder: "(optional)",
        expectedPlaceholder: "200",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: false,
      },
      {
        value: "header_equals",
        category: "contract_cross_step",
        labelZh: "响应头匹配 (header_equals)",
        labelEn: "Response Header Equals",
        jsonpathLabelZh: "响应头名称 (Header Name)",
        jsonpathLabelEn: "Response Header Name",
        jsonpathPlaceholder: "Content-Type",
        expectedPlaceholder: "application/json",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "latency_lt",
        category: "contract_cross_step",
        labelZh: "响应耗时小于 ms (latency_lt)",
        labelEn: "Latency < ms",
        jsonpathLabelZh: "路径（可选）",
        jsonpathLabelEn: "Path (optional)",
        jsonpathPlaceholder: "(optional)",
        expectedPlaceholder: "500",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: false,
        hintZh: "填写毫秒阈值（如 500 表示耗时需小于 500ms）",
        hintEn: "Max allowed latency in milliseconds (e.g., 500)",
      },
      {
        value: "json_schema",
        category: "contract_cross_step",
        labelZh: "JSON Schema 契约校验",
        labelEn: "JSON Schema Contract",
        jsonpathLabelZh: "目标 JSONPath（默认 $）",
        jsonpathLabelEn: "Target JSONPath (default $)",
        jsonpathPlaceholder: "$.data",
        expectedPlaceholder: '{"type":"object","required":["id","status"]}',
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
      {
        value: "var_equals",
        category: "contract_cross_step",
        labelZh: "跨步骤变量表达式 (var_equals)",
        labelEn: "Cross-Step Var Expression",
        jsonpathLabelZh: "实际值 JSONPath 或 {{变量}}",
        jsonpathLabelEn: "Actual JSONPath or {{var}}",
        jsonpathPlaceholder: "$.data.balance",
        expectedPlaceholder: "{{before_balance}} - 50",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
        hintZh: "支持引用上游提取的 {{var}} 并执行简单算术表达式比对",
        hintEn: "Supports {{var}} interpolation and simple arithmetic expressions",
      },
    ],
  },
  {
    id: "database_readonly",
    labelZh: "只读数据库核对 (SQL)",
    labelEn: "Read-Only Database Check (SQL)",
    operators: [
      {
        value: "sql_equals",
        category: "database_readonly",
        labelZh: "SQL 查询值等于 (sql_equals)",
        labelEn: "SQL Scalar Equals",
        jsonpathLabelZh: "只读 SQL (SELECT)",
        jsonpathLabelEn: "Read-Only SQL (SELECT)",
        jsonpathPlaceholder: "SELECT status FROM orders WHERE id = {{order_id}}",
        expectedPlaceholder: "PAID",
        needsDbDsn: true,
        needsExpected: true,
        needsJsonpath: true,
        hintZh: "仅允许只读 SELECT 语句，支持 {{var}} 参数替换",
        hintEn: "Read-only SELECT only; supports {{var}} substitution",
      },
      {
        value: "sql_exists",
        category: "database_readonly",
        labelZh: "SQL 记录存在 (sql_exists)",
        labelEn: "SQL Row Exists",
        jsonpathLabelZh: "只读 SQL (SELECT)",
        jsonpathLabelEn: "Read-Only SQL (SELECT)",
        jsonpathPlaceholder: "SELECT status FROM orders WHERE id = {{order_id}}",
        expectedPlaceholder: "(optional — row must exist)",
        needsDbDsn: true,
        needsExpected: false,
        needsJsonpath: true,
        hintZh: "查询返回至少 1 行即视为通过（仅允许 SELECT）",
        hintEn: "Passes when SELECT returns at least 1 row (read-only)",
      },
    ],
  },
  {
    id: "custom",
    labelZh: "自定义表达式",
    labelEn: "Custom",
    operators: [
      {
        value: "custom",
        category: "custom",
        labelZh: "自定义规则 (custom)",
        labelEn: "Custom Rule",
        jsonpathLabelZh: "JSONPath / 目标字段",
        jsonpathLabelEn: "JSONPath / Target",
        jsonpathPlaceholder: "$.data",
        expectedPlaceholder: "Describe custom assertion rule",
        needsDbDsn: false,
        needsExpected: true,
        needsJsonpath: true,
      },
    ],
  },
];

const OPERATOR_META_MAP = new Map<ScenarioAssertionOperator, AssertionOperatorMeta>();
for (const group of ASSERTION_OPERATOR_GROUPS) {
  for (const op of group.operators) {
    OPERATOR_META_MAP.set(op.value, op);
  }
}

export const ALL_ASSERTION_OPERATORS: ScenarioAssertionOperator[] = Array.from(
  OPERATOR_META_MAP.keys()
);

const FALLBACK_META: AssertionOperatorMeta = OPERATOR_META_MAP.get("custom")!;

export function getOperatorMeta(op: ScenarioAssertionOperator | string): AssertionOperatorMeta {
  return OPERATOR_META_MAP.get(op as ScenarioAssertionOperator) ?? FALLBACK_META;
}

export function isSqlOperator(op: ScenarioAssertionOperator | string): boolean {
  return op === "sql_equals" || op === "sql_exists";
}

export function computeResumeStepIndex(
  chainRun: Pick<ChainRunOut, "failed_step_index" | "completed_steps" | "total_steps"> | null | undefined
): number {
  if (!chainRun || chainRun.total_steps <= 0) {
    return 0;
  }
  const maxIdx = Math.max(0, chainRun.total_steps - 1);
  if (typeof chainRun.failed_step_index === "number" && Number.isFinite(chainRun.failed_step_index)) {
    return Math.max(0, Math.min(maxIdx, Math.trunc(chainRun.failed_step_index)));
  }
  if (
    typeof chainRun.completed_steps === "number" &&
    Number.isFinite(chainRun.completed_steps) &&
    chainRun.completed_steps >= 0 &&
    chainRun.completed_steps < chainRun.total_steps
  ) {
    return Math.max(0, Math.min(maxIdx, Math.trunc(chainRun.completed_steps)));
  }
  return 0;
}

const TEMPLATE_VAR_RE = /\{\{\s*([a-zA-Z_][a-zA-Z0-9_.-]*)\s*\}\}/g;

function collectTemplateVars(input: unknown, seen: Set<string>, out: string[]): void {
  if (typeof input === "string") {
    TEMPLATE_VAR_RE.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = TEMPLATE_VAR_RE.exec(input)) !== null) {
      const name = match[1];
      if (name && !seen.has(name)) {
        seen.add(name);
        out.push(name);
      }
    }
    return;
  }
  if (Array.isArray(input)) {
    for (const item of input) {
      collectTemplateVars(item, seen, out);
    }
    return;
  }
  if (input && typeof input === "object") {
    for (const [key, val] of Object.entries(input as Record<string, unknown>)) {
      collectTemplateVars(key, seen, out);
      collectTemplateVars(val, seen, out);
    }
  }
}

export function extractTemplateVars(input: unknown): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  collectTemplateVars(input, seen, out);
  return out;
}

export function matchesChainCheckpoint(
  cp: Pick<ChainRunOut, "chain_index" | "chain_name">,
  chainIndex: number,
  chainName: string
): boolean {
  if (typeof cp.chain_index === "number" && Number.isFinite(cp.chain_index)) {
    return cp.chain_index === chainIndex;
  }
  return cp.chain_name === chainName;
}

function normalizeEndpointPath(rawPath: string): string {
  const noQuery = rawPath.split("?")[0]?.trim() ?? "";
  return noQuery.replace(/\/+$/, "") || "/";
}

export function filterScenarioAssertionsForEndpoint(
  assertions: ScenarioAssertion[],
  method: string,
  path: string
): ScenarioAssertion[] {
  const normMethod = method.trim().toUpperCase();
  const normPath = normalizeEndpointPath(path);

  return assertions.filter((asrt) => {
    const target = (asrt.target_endpoint ?? "").trim();
    if (!target) {
      return true;
    }
    const parts = target.split(/\s+/);
    if (parts.length >= 2) {
      const targetMethod = parts[0].toUpperCase();
      const targetPath = normalizeEndpointPath(parts.slice(1).join(" "));
      return targetMethod === normMethod && targetPath === normPath;
    }
    return normalizeEndpointPath(target) === normPath;
  });
}
