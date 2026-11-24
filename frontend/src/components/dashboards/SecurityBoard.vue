<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  ChevronDown,
  ChevronRight,
  Cookie,
  ExternalLink,
  KeyRound,
  Loader2,
  Play,
  Radar,
  RefreshCw,
  Search,
  Shield,
  ShieldAlert,
  ShieldCheck,
} from "lucide-vue-next";
import { listAuthProfiles } from "@/api/projects";
import { getRunSecurity, listRunFindings } from "@/api/reports";
import { executeRunModules, getRun } from "@/api/runs";
import type { AuthProfile, Finding, SecurityReport } from "@/api/types";
import BurpExportDrawer from "@/components/dashboards/BurpExportDrawer.vue";
import EmptyState from "@/components/layout/EmptyState.vue";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import { useI18n } from "@/i18n";
import "@/i18n/locales/dashboards";

interface HeaderCoverageCell {
  present: number;
  total: number;
  coverage: number;
}

interface HostHeaderRow {
  host: string;
  seen: number;
  hsts: HeaderCoverageCell;
  csp: HeaderCoverageCell;
  xfo: HeaderCoverageCell;
  xcto: HeaderCoverageCell;
  referrer: HeaderCoverageCell;
  corsWildcard: number;
  corsDangerous: number;
  servers: string[];
}

interface CookieIssueRow {
  host: string;
  endpoint: string;
  cookieName: string;
  missingFlags: string[];
  severity: string;
}

interface LeakSignalRow {
  kind: string;
  source: string;
  sample: string;
  context: string;
  severity: string;
}

interface NormalizedAuthRow {
  key: string;
  checkType: "unauth" | "idor" | "auth";
  method: string;
  endpoint: string;
  url: string;
  verdict: string;
  baselineStatus: number | null;
  replayStatus: number | null;
  similarity: number | null;
  idParams: string[];
  isMutation: boolean;
  error: string | null;
}

interface NormalizedNucleiRow {
  templateId: string;
  name: string;
  severity: string;
  matchedAt: string;
  tags: string[];
}

const props = defineProps<{
  runId: string;
  projectId?: string;
}>();

const emit = defineEmits<{
  (e: "executed"): void;
}>();

const { t } = useI18n();

const loading = ref(true);
const executing = ref(false);
const error = ref<string | null>(null);
const execError = ref<string | null>(null);
const security = ref<SecurityReport | null>(null);
const dbFindings = ref<Finding[]>([]);

const authProfiles = ref<AuthProfile[]>([]);
const authAProfileId = ref("");
const authBProfileId = ref("");
const scanTargetUrl = ref("");
const includeMutations = ref(false);
const runNuclei = ref(false);
let isUnmounted = false;

const severityFilter = ref<string>("all");
const categoryFilter = ref<string>("all");
const findingSearch = ref("");
const expandedFindingIds = ref<Set<string>>(new Set());

const authVerdictFilter = ref<string>("all");
const authSearch = ref("");
const selectedAuthKeys = ref<Set<string>>(new Set());

const burpDrawerOpen = ref(false);
const burpTargetEndpoints = ref<string[]>([]);

const severityCards = computed(() => [
  {
    key: "critical",
    label: t("sec.sevCritical"),
    ring: "border-rose-300 bg-rose-50/50",
    text: "text-rose-700",
    bar: "bg-rose-600",
  },
  {
    key: "high",
    label: t("sec.sevHigh"),
    ring: "border-orange-200 bg-orange-50/40",
    text: "text-orange-700",
    bar: "bg-orange-500",
  },
  {
    key: "medium",
    label: t("sec.sevMedium"),
    ring: "border-amber-200 bg-amber-50/40",
    text: "text-amber-700",
    bar: "bg-amber-500",
  },
  {
    key: "low",
    label: t("sec.sevLow"),
    ring: "border-sky-200 bg-sky-50/40",
    text: "text-sky-700",
    bar: "bg-sky-500",
  },
  {
    key: "info",
    label: t("sec.sevInfo"),
    ring: "border-slate-200 bg-white",
    text: "text-slate-700",
    bar: "bg-slate-400",
  },
]);

function extractCell(
  cov: Record<string, unknown> | undefined,
  headerKey: string,
  seen: number
): HeaderCoverageCell {
  const entry = cov?.[headerKey];
  if (entry && typeof entry === "object") {
    const obj = entry as Record<string, unknown>;
    const present = Number(obj.present ?? 0);
    const total = Number(obj.total ?? seen);
    const ratio =
      typeof obj.coverage === "number" ? obj.coverage : total > 0 ? present / total : 0;
    return { present, total, coverage: ratio };
  }
  if (typeof entry === "boolean") {
    return { present: entry ? seen || 1 : 0, total: seen || 1, coverage: entry ? 1 : 0 };
  }
  if (typeof entry === "number") {
    return { present: entry, total: seen || 1, coverage: seen > 0 ? entry / seen : entry };
  }
  return { present: 0, total: seen, coverage: 0 };
}

const hostHeaderRows = computed<HostHeaderRow[]>(() => {
  const audit = security.value?.static_audit as Record<string, unknown> | null | undefined;
  if (!audit) return [];

  const rawMap = (audit.host_headers ?? audit.header_coverage) as
    | Record<string, Record<string, unknown>>
    | undefined;
  if (!rawMap || typeof rawMap !== "object") return [];

  return Object.entries(rawMap).map(([host, data]) => {
    const seen = Number(data.seen ?? data.total ?? 0);
    const cov = (data.coverage ?? data) as Record<string, unknown>;
    return {
      host,
      seen,
      hsts: extractCell(cov, "strict-transport-security", seen),
      csp: extractCell(cov, "content-security-policy", seen),
      xfo: extractCell(cov, "x-frame-options", seen),
      xcto: extractCell(cov, "x-content-type-options", seen),
      referrer: extractCell(cov, "referrer-policy", seen),
      corsWildcard: Number(data.cors_wildcard ?? 0),
      corsDangerous: Number(data.cors_dangerous ?? 0),
      servers: Array.isArray(data.servers) ? data.servers.map(String) : [],
    };
  });
});

const cookieIssues = computed<CookieIssueRow[]>(() => {
  const audit = security.value?.static_audit as Record<string, unknown> | null | undefined;
  const raw = audit?.cookie_issues;
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => {
    const obj = (item ?? {}) as Record<string, unknown>;
    return {
      host: String(obj.host ?? ""),
      endpoint: String(obj.endpoint ?? ""),
      cookieName: String(obj.cookie_name ?? obj.name ?? "cookie"),
      missingFlags: Array.isArray(obj.missing_flags) ? obj.missing_flags.map(String) : [],
      severity: String(obj.severity ?? "medium").toLowerCase(),
    };
  });
});

const leakSignals = computed<LeakSignalRow[]>(() => {
  const audit = security.value?.static_audit as Record<string, unknown> | null | undefined;
  if (!audit) return [];
  const out: LeakSignalRow[] = [];

  const rawLeaks = audit.sensitive_leaks;
  if (rawLeaks && typeof rawLeaks === "object" && !Array.isArray(rawLeaks)) {
    for (const [kind, items] of Object.entries(rawLeaks as Record<string, unknown>)) {
      if (!Array.isArray(items)) continue;
      for (const it of items) {
        const obj = (it ?? {}) as Record<string, unknown>;
        out.push({
          kind,
          source: String(obj.source ?? ""),
          sample: String(obj.sample ?? "***REDACTED***"),
          context: typeof obj.context === "string" ? obj.context : "",
          severity: String(obj.severity ?? "medium").toLowerCase(),
        });
      }
    }
  }

  if (out.length === 0 && Array.isArray(audit.secret_findings)) {
    for (const it of audit.secret_findings) {
      const obj = (it ?? {}) as Record<string, unknown>;
      out.push({
        kind: String(obj.leak_type ?? obj.kind ?? "secret"),
        source: String(obj.source ?? obj.endpoint ?? ""),
        sample: String(obj.sample ?? obj.redacted_sample ?? "***REDACTED***"),
        context: typeof obj.context === "string" ? obj.context : "",
        severity: String(obj.severity ?? "high").toLowerCase(),
      });
    }
  }
  return out;
});

const authMatrixRows = computed<NormalizedAuthRow[]>(() => {
  const am = security.value?.auth_matrix as Record<string, unknown> | null | undefined;
  if (!am) return [];

  const rows: NormalizedAuthRow[] = [];
  const pushList = (list: unknown, checkType: "unauth" | "idor" | "auth") => {
    if (!Array.isArray(list)) return;
    list.forEach((item, idx) => {
      const obj = (item ?? {}) as Record<string, unknown>;
      const method = String(obj.method ?? "GET").toUpperCase();
      const endpoint = String(obj.endpoint ?? obj.normalized_path ?? obj.url ?? "/");
      const url = String(obj.url ?? "");
      const simRaw = obj.similarity;
      rows.push({
        key: `${checkType}-${idx}-${method}-${endpoint}`,
        checkType,
        method,
        endpoint,
        url,
        verdict: String(obj.verdict ?? "blocked").toLowerCase(),
        baselineStatus: typeof obj.baseline_status === "number" ? obj.baseline_status : null,
        replayStatus:
          typeof obj.replay_status === "number"
            ? obj.replay_status
            : typeof obj.unauth_status === "number"
              ? obj.unauth_status
              : null,
        similarity: typeof simRaw === "number" ? simRaw : null,
        idParams: Array.isArray(obj.id_params) ? obj.id_params.map(String) : [],
        isMutation: Boolean(obj.is_mutation),
        error: typeof obj.error === "string" ? obj.error : null,
      });
    });
  };

  pushList(am.unauth_results, "unauth");
  pushList(am.idor_results, "idor");
  if (rows.length === 0) {
    pushList(am.results, "auth");
  }
  return rows;
});

const authSummary = computed(() => {
  const am = security.value?.auth_matrix as Record<string, unknown> | null | undefined;
  const s = (am?.summary && typeof am.summary === "object"
    ? (am.summary as Record<string, unknown>)
    : am) as Record<string, unknown> | undefined;

  const countVerdict = (v: string) =>
    authMatrixRows.value.filter((row) => row.verdict === v).length;

  return {
    authAConfigured: Boolean(s?.auth_a_configured),
    authBConfigured: Boolean(s?.auth_b_configured),
    unauthBypass: Number(s?.unauth_bypass ?? s?.unauth_bypass_count ?? countVerdict("unauth_bypass")),
    confirmedIdor: Number(
      s?.confirmed_idor ?? s?.confirmed_idor_count ?? countVerdict("confirmed_idor")
    ),
    suspectedIdor: Number(
      s?.suspected_idor ?? s?.suspected_idor_count ?? countVerdict("suspected_idor")
    ),
    mutationCandidates: Number(
      s?.mutation_candidates ?? s?.mutation_candidate_count ?? countVerdict("mutation_candidate")
    ),
    blocked: Number(s?.blocked ?? countVerdict("blocked")),
  };
});

const filteredAuthRows = computed(() => {
  const q = authSearch.value.trim().toLowerCase();
  return authMatrixRows.value.filter((row) => {
    if (authVerdictFilter.value !== "all" && row.verdict !== authVerdictFilter.value) {
      return false;
    }
    if (!q) return true;
    return (
      row.endpoint.toLowerCase().includes(q) ||
      row.method.toLowerCase().includes(q) ||
      row.verdict.toLowerCase().includes(q) ||
      row.idParams.some((p) => p.toLowerCase().includes(q))
    );
  });
});

const nucleiRows = computed<NormalizedNucleiRow[]>(() => {
  const raw = security.value?.nuclei;
  if (!Array.isArray(raw)) return [];
  return raw.map((rec) => {
    const info =
      rec.info && typeof rec.info === "object" ? (rec.info as Record<string, unknown>) : {};
    const templateId = String(rec["template-id"] ?? rec.templateID ?? rec.template_id ?? "nuclei");
    return {
      templateId,
      name: String(info.name ?? rec.name ?? templateId),
      severity: String(info.severity ?? rec.severity ?? "info").toLowerCase(),
      matchedAt: String(rec["matched-at"] ?? rec.host ?? rec.endpoint ?? ""),
      tags: Array.isArray(info.tags)
        ? info.tags.map(String)
        : typeof info.tags === "string"
          ? info.tags.split(",").map((tag) => tag.trim()).filter(Boolean)
          : [],
    };
  });
});

const allSecurityFindings = computed<Finding[]>(() => {
  const nonPerfDb = dbFindings.value.filter(
    (f) => !String(f.category || "").toLowerCase().startsWith("perf_")
  );
  if (nonPerfDb.length > 0) return nonPerfDb;

  const combined: Finding[] = [];
  const pushInline = (list: unknown, prefix: string) => {
    if (!Array.isArray(list)) return;
    list.forEach((raw, idx) => {
      const obj = (raw ?? {}) as Record<string, unknown>;
      combined.push({
        id: `${prefix}-${idx}`,
        run_id: props.runId,
        category: String(obj.category ?? "security"),
        severity: String(obj.severity ?? "info").toLowerCase(),
        endpoint: String(obj.endpoint ?? ""),
        title: String(obj.title ?? "Security Finding"),
        evidence:
          obj.evidence && typeof obj.evidence === "object"
            ? (obj.evidence as Record<string, unknown>)
            : {},
      });
    });
  };
  pushInline(security.value?.static_audit?.findings, "static");
  pushInline(security.value?.auth_matrix?.findings, "auth");
  return combined;
});

const severityCounts = computed(() => {
  const counts: Record<string, number> = {
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    info: 0,
  };
  for (const f of allSecurityFindings.value) {
    const sev = String(f.severity || "info").toLowerCase();
    if (sev in counts) counts[sev] += 1;
    else counts.info += 1;
  }
  return counts;
});

const findingCategories = computed(() => {
  const set = new Set<string>();
  for (const f of allSecurityFindings.value) {
    if (f.category) set.add(f.category);
  }
  return Array.from(set).sort();
});

const filteredFindings = computed(() => {
  const q = findingSearch.value.trim().toLowerCase();
  return allSecurityFindings.value.filter((f) => {
    const sev = String(f.severity || "info").toLowerCase();
    if (severityFilter.value !== "all" && sev !== severityFilter.value) return false;
    if (categoryFilter.value !== "all" && f.category !== categoryFilter.value) return false;
    if (!q) return true;
    return (
      f.title.toLowerCase().includes(q) ||
      f.endpoint.toLowerCase().includes(q) ||
      f.category.toLowerCase().includes(q)
    );
  });
});

function checkTypeLabel(kind: "unauth" | "idor" | "auth"): string {
  if (kind === "idor") return t("sec.checkDualIdor");
  if (kind === "unauth") return t("sec.checkUnauth");
  return t("sec.checkAuth");
}

function pillClassForHeader(cell: HeaderCoverageCell): string {
  if (cell.total > 0 && cell.present === cell.total) {
    return "bg-emerald-50 text-emerald-700 ring-emerald-600/20";
  }
  if (cell.present > 0) {
    return "bg-amber-50 text-amber-800 ring-amber-600/20";
  }
  return "bg-rose-50 text-rose-700 ring-rose-600/20";
}

function pillLabelForHeader(cell: HeaderCoverageCell): string {
  if (cell.total === 0) return "N/A";
  if (cell.present === 0) return t("sec.headerMissing");
  if (cell.present === cell.total) {
    return t("sec.headerPass", { present: cell.present, total: cell.total });
  }
  return `${Math.round(cell.coverage * 100)}% (${cell.present}/${cell.total})`;
}

function methodBadgeClass(method: string): string {
  switch (method.toUpperCase()) {
    case "GET":
      return "bg-sky-50 text-sky-700 ring-sky-200";
    case "POST":
      return "bg-emerald-50 text-emerald-700 ring-emerald-200";
    case "PUT":
    case "PATCH":
      return "bg-amber-50 text-amber-700 ring-amber-200";
    case "DELETE":
      return "bg-rose-50 text-rose-700 ring-rose-200";
    default:
      return "bg-slate-100 text-slate-700 ring-slate-200";
  }
}

function toggleFindingExpand(id: string): void {
  const next = new Set(expandedFindingIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  expandedFindingIds.value = next;
}

function toggleAuthSelection(key: string): void {
  const next = new Set(selectedAuthKeys.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  selectedAuthKeys.value = next;
}

function openBurpForSelected(): void {
  const chosen = authMatrixRows.value
    .filter((row) => selectedAuthKeys.value.has(row.key))
    .map((row) => row.url || row.endpoint);
  burpTargetEndpoints.value = chosen;
  burpDrawerOpen.value = true;
}

function openBurpForSingle(row: NormalizedAuthRow): void {
  burpTargetEndpoints.value = [row.url || row.endpoint];
  burpDrawerOpen.value = true;
}

async function loadSecurityData(): Promise<void> {
  if (!props.runId) return;
  loading.value = true;
  error.value = null;

  const [secRes, findingsRes] = await Promise.allSettled([
    getRunSecurity(props.runId),
    listRunFindings(props.runId),
  ]);

  if (secRes.status === "fulfilled") {
    security.value = secRes.value;
  } else {
    security.value = null;
    const msg = secRes.reason instanceof Error ? secRes.reason.message : "";
    if (!msg.includes("404") && !msg.toLowerCase().includes("not found")) {
      error.value = msg || t("sec.errorFallback");
    }
  }

  if (findingsRes.status === "fulfilled") {
    dbFindings.value = findingsRes.value;
  } else {
    dbFindings.value = [];
  }

  loading.value = false;
}

async function loadProjectAuthProfiles(): Promise<void> {
  let pid = props.projectId || "";
  if (!pid && props.runId) {
    try {
      const run = await getRun(props.runId);
      pid = run.project_id;
    } catch {
      return;
    }
  }
  if (!pid) return;
  try {
    const profiles = await listAuthProfiles(pid);
    authProfiles.value = profiles;
    if (!authAProfileId.value) {
      const a = profiles.find((p) => p.role_tag === "account_a");
      if (a) authAProfileId.value = a.id;
    }
    if (!authBProfileId.value) {
      const b = profiles.find((p) => p.role_tag === "account_b");
      if (b) authBProfileId.value = b.id;
    }
  } catch {
    authProfiles.value = [];
  }
}

async function handleExecuteSecurity(mode: "static" | "full"): Promise<void> {
  if (!props.runId || executing.value) return;
  executing.value = true;
  execError.value = null;
  try {
    const config: Record<string, unknown> =
      mode === "static"
        ? { include_mutations: false, run_nuclei: false }
        : {
            auth_a_profile_id: authAProfileId.value || null,
            auth_b_profile_id: authBProfileId.value || null,
            target_url: scanTargetUrl.value.trim() || null,
            include_mutations: includeMutations.value,
            run_nuclei: runNuclei.value,
          };
    await executeRunModules(props.runId, { modules: ["security"], config });
    let finalStatus = "";
    for (let i = 0; i < 100 && !isUnmounted; i += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 600));
      if (isUnmounted) return;
      const run = await getRun(props.runId);
      finalStatus = run.status;
      if (run.status === "completed" || run.status === "failed") {
        break;
      }
    }
    if (isUnmounted) return;
    if (finalStatus === "failed") {
      execError.value = t("sec.execFailed");
    }
    await loadSecurityData();
    emit("executed");
  } catch (err) {
    execError.value = err instanceof Error ? err.message : t("sec.execFailed");
  } finally {
    executing.value = false;
  }
}

watch(
  () => props.runId,
  () => {
    selectedAuthKeys.value = new Set();
    expandedFindingIds.value = new Set();
    execError.value = null;
    void loadSecurityData();
    void loadProjectAuthProfiles();
  }
);

watch(
  () => props.projectId,
  () => {
    void loadProjectAuthProfiles();
  }
);

onMounted(() => {
  isUnmounted = false;
  void loadSecurityData();
  void loadProjectAuthProfiles();
});

onBeforeUnmount(() => {
  isUnmounted = true;
});
</script>

<template>
  <div class="space-y-6">
    <!-- Contextual On-Demand Security & Dual-Account A/B IDOR Bar -->
    <div class="card p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <h3 class="text-sm font-semibold text-slate-900">{{ t("sec.actionTitle") }}</h3>
          <p class="mt-0.5 text-xs text-slate-500">{{ t("sec.actionDesc") }}</p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="sec-run-static-btn"
            class="btn-secondary py-1.5 text-xs"
            :disabled="executing"
            @click="handleExecuteSecurity('static')"
          >
            <Loader2 v-if="executing" class="h-3.5 w-3.5 animate-spin" />
            <ShieldCheck v-else class="h-3.5 w-3.5 text-indigo-600" />
            <span>{{ executing ? t("sec.runningBtn") : t("sec.runStaticBtn") }}</span>
          </button>
          <button
            type="button"
            data-testid="sec-run-full-btn"
            class="btn-primary py-1.5 text-xs"
            :disabled="executing"
            @click="handleExecuteSecurity('full')"
          >
            <Loader2 v-if="executing" class="h-3.5 w-3.5 animate-spin" />
            <Play v-else class="h-3.5 w-3.5" />
            <span>{{ executing ? t("sec.runningBtn") : t("sec.runFullBtn") }}</span>
          </button>
        </div>
      </div>

      <!-- Dual-Account A/B & Active Scan Config Controls -->
      <div class="mt-3.5 grid grid-cols-1 gap-3 border-t border-slate-100 pt-3.5 sm:grid-cols-3">
        <div>
          <label class="block text-[11px] font-medium text-slate-600">
            {{ t("sec.accountALabel") }}
          </label>
          <select
            v-model="authAProfileId"
            data-testid="sec-auth-a-select"
            class="input-field mt-1 py-1.5 text-xs"
            :disabled="executing"
          >
            <option value="">{{ t("sec.accountNone") }}</option>
            <option v-for="p in authProfiles" :key="p.id" :value="p.id">
              {{ p.name }} ({{ p.role_tag }})
            </option>
          </select>
        </div>

        <div>
          <label class="block text-[11px] font-medium text-slate-600">
            {{ t("sec.accountBLabel") }}
          </label>
          <select
            v-model="authBProfileId"
            data-testid="sec-auth-b-select"
            class="input-field mt-1 py-1.5 text-xs"
            :disabled="executing"
          >
            <option value="">{{ t("sec.accountNone") }}</option>
            <option v-for="p in authProfiles" :key="p.id" :value="p.id">
              {{ p.name }} ({{ p.role_tag }})
            </option>
          </select>
        </div>

        <div>
          <label class="block text-[11px] font-medium text-slate-600">
            {{ t("sec.targetUrlLabel") }}
          </label>
          <input
            v-model="scanTargetUrl"
            type="text"
            data-testid="sec-target-url-input"
            class="input-field mt-1 py-1.5 font-mono text-xs"
            :placeholder="t('sec.targetUrlPlaceholder')"
            :disabled="executing"
          />
        </div>
      </div>

      <div class="mt-3 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div class="flex flex-wrap items-center gap-4">
          <label class="inline-flex cursor-pointer items-center gap-1.5 text-slate-700">
            <input
              v-model="includeMutations"
              type="checkbox"
              data-testid="sec-include-mutations"
              class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
              :disabled="executing"
            />
            <span>{{ t("sec.includeMutations") }}</span>
          </label>
          <label class="inline-flex cursor-pointer items-center gap-1.5 text-slate-700">
            <input
              v-model="runNuclei"
              type="checkbox"
              data-testid="sec-run-nuclei"
              class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
              :disabled="executing"
            />
            <span>{{ t("sec.runNuclei") }}</span>
          </label>
        </div>

        <div v-if="authProfiles.length < 2" class="flex items-center gap-2 text-[11px] text-slate-500">
          <span>{{ t("sec.noDualAccountsHint") }}</span>
          <router-link
            :to="projectId ? `/p/${projectId}/settings?tab=credentials` : '/projects'"
            class="font-medium text-indigo-600 hover:text-indigo-800"
          >
            {{ t("sec.configureAuthLink") }}
          </router-link>
        </div>
      </div>

      <div
        v-if="execError"
        class="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700"
      >
        {{ execError }}
      </div>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="card flex items-center justify-center gap-2.5 py-16 text-sm text-slate-500">
      <Loader2 class="h-5 w-5 animate-spin text-indigo-600" />
      <span>{{ t("sec.loading") }}</span>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="card border-rose-200 bg-rose-50/70 p-6">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h3 class="text-sm font-semibold text-rose-800">{{ t("sec.errorTitle") }}</h3>
          <p class="mt-1 text-xs text-rose-700">{{ error }}</p>
        </div>
        <button type="button" class="btn-secondary text-xs" @click="loadSecurityData">
          <RefreshCw class="h-3.5 w-3.5" />
          <span>{{ t("java.retry") }}</span>
        </button>
      </div>
    </div>

    <!-- Empty state -->
    <EmptyState
      v-else-if="!security && allSecurityFindings.length === 0"
      :title="t('sec.emptyTitle')"
      :description="t('sec.emptyDesc')"
    >
      <template #icon>
        <Shield class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <button type="button" class="btn-secondary text-xs" @click="loadSecurityData">
          <RefreshCw class="h-3.5 w-3.5" />
          <span>{{ t("java.refresh") }}</span>
        </button>
      </template>
    </EmptyState>

    <template v-else>
      <!-- 1. Severity Summary Cards (clickable filters) -->
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <button
          v-for="item in severityCards"
          :key="item.key"
          type="button"
          class="relative overflow-hidden rounded-xl border p-4 text-left shadow-sm transition hover:shadow"
          :class="[
            item.ring,
            severityFilter === item.key ? 'ring-2 ring-indigo-500 ring-offset-1' : '',
          ]"
          @click="severityFilter = severityFilter === item.key ? 'all' : item.key"
        >
          <div class="absolute inset-y-0 left-0 w-1" :class="item.bar" />
          <div class="pl-2">
            <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              {{ item.label }}
            </p>
            <p class="mt-1 text-2xl font-semibold tabular-nums" :class="item.text">
              {{ severityCounts[item.key] }}
            </p>
          </div>
        </button>
      </div>

      <!-- 2. Auth & Dual-Account A/B IDOR Matrix -->
      <div class="card overflow-hidden">
        <div class="card-header flex-wrap">
          <div class="flex items-center gap-2.5">
            <ShieldAlert class="h-4 w-4 text-rose-600" />
            <div>
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("sec.authTitle") }}
              </h3>
              <p class="text-xs text-slate-500">
                {{ t("sec.authDesc") }}
              </p>
            </div>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <button
              type="button"
              class="btn-primary bg-amber-600 text-xs hover:bg-amber-700"
              @click="openBurpForSelected"
            >
              <ExternalLink class="h-3.5 w-3.5" />
              <span>
                {{ t("sec.exportBurpBtn") }}
                {{ selectedAuthKeys.size > 0 ? `(${selectedAuthKeys.size})` : "" }}
              </span>
            </button>
          </div>
        </div>

        <!-- Summary bar + filters -->
        <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 bg-slate-50/70 px-5 py-3">
          <div class="flex flex-wrap items-center gap-2 text-xs">
            <span class="rounded-md bg-white px-2 py-1 text-slate-600 ring-1 ring-slate-200">
              {{ t("sec.statUnauthBypass") }}:
              <strong class="ml-1 text-rose-700">{{ authSummary.unauthBypass }}</strong>
            </span>
            <span class="rounded-md bg-white px-2 py-1 text-slate-600 ring-1 ring-slate-200">
              {{ t("sec.statConfirmedIdor") }}:
              <strong class="ml-1 text-rose-700">{{ authSummary.confirmedIdor }}</strong>
            </span>
            <span class="rounded-md bg-white px-2 py-1 text-slate-600 ring-1 ring-slate-200">
              {{ t("sec.statSuspectedIdor") }}:
              <strong class="ml-1 text-amber-700">{{ authSummary.suspectedIdor }}</strong>
            </span>
            <span class="rounded-md bg-white px-2 py-1 text-slate-600 ring-1 ring-slate-200">
              {{ t("sec.statMutationCand") }}:
              <strong class="ml-1 text-amber-700">{{ authSummary.mutationCandidates }}</strong>
            </span>
            <span class="rounded-md bg-white px-2 py-1 text-slate-600 ring-1 ring-slate-200">
              {{ t("sec.statBlocked") }}:
              <strong class="ml-1 text-emerald-700">{{ authSummary.blocked }}</strong>
            </span>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <div class="relative">
              <Search class="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
              <input
                v-model="authSearch"
                type="text"
                :placeholder="t('sec.searchEndpoint')"
                class="input-field py-1 pl-8 pr-3 text-xs"
              />
            </div>
            <select v-model="authVerdictFilter" class="input-field w-auto py-1 text-xs">
              <option value="all">{{ t("sec.verdictAll") }}</option>
              <option value="confirmed_idor">{{ t("sec.verdictConfirmedIdor") }}</option>
              <option value="unauth_bypass">{{ t("sec.verdictUnauthBypass") }}</option>
              <option value="suspected_idor">{{ t("sec.verdictSuspectedIdor") }}</option>
              <option value="mutation_candidate">{{ t("sec.verdictMutation") }}</option>
              <option value="blocked">{{ t("sec.verdictBlocked") }}</option>
              <option value="skipped_no_auth_b">{{ t("sec.verdictSkippedNoB") }}</option>
            </select>
          </div>
        </div>

        <div v-if="filteredAuthRows.length === 0" class="px-5 py-10 text-center text-xs text-slate-500">
          {{ authMatrixRows.length === 0 ? t("sec.authEmpty") : t("sec.authNoMatch") }}
        </div>

        <div v-else class="overflow-x-auto">
          <table class="min-w-full divide-y divide-slate-200 text-xs">
            <thead class="table-head">
              <tr>
                <th class="w-10 px-4 py-2.5" />
                <th class="px-3 py-2.5">{{ t("sec.colCheck") }}</th>
                <th class="px-3 py-2.5">{{ t("sec.colEndpoint") }}</th>
                <th class="px-3 py-2.5">{{ t("sec.colVerdict") }}</th>
                <th class="px-3 py-2.5 text-center">{{ t("sec.colStatusFlow") }}</th>
                <th class="px-3 py-2.5">{{ t("sec.colSimilarity") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("sec.colAction") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 bg-white">
              <tr v-for="row in filteredAuthRows" :key="row.key" class="hover:bg-slate-50/70">
                <td class="px-4 py-2.5">
                  <input
                    type="checkbox"
                    class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                    :checked="selectedAuthKeys.has(row.key)"
                    @change="toggleAuthSelection(row.key)"
                  />
                </td>
                <td class="whitespace-nowrap px-3 py-2.5">
                  <span
                    class="rounded px-2 py-0.5 text-[11px] font-medium"
                    :class="
                      row.checkType === 'idor'
                        ? 'bg-violet-50 text-violet-700'
                        : 'bg-slate-100 text-slate-700'
                    "
                  >
                    {{ checkTypeLabel(row.checkType) }}
                  </span>
                </td>
                <td class="px-3 py-2.5">
                  <div class="flex items-center gap-2">
                    <span
                      class="rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ring-1 ring-inset"
                      :class="methodBadgeClass(row.method)"
                    >
                      {{ row.method }}
                    </span>
                    <span class="max-w-md truncate font-mono text-slate-800" :title="row.endpoint">
                      {{ row.endpoint }}
                    </span>
                  </div>
                  <div v-if="row.idParams.length > 0" class="mt-1 flex flex-wrap gap-1">
                    <span
                      v-for="param in row.idParams"
                      :key="param"
                      class="rounded bg-indigo-50 px-1.5 py-0.5 font-mono text-[10px] text-indigo-700"
                    >
                      {{ param }}
                    </span>
                  </div>
                </td>
                <td class="whitespace-nowrap px-3 py-2.5">
                  <StatusBadge :status="row.verdict" />
                </td>
                <td class="whitespace-nowrap px-3 py-2.5 text-center font-mono text-slate-700">
                  <span>{{ row.baselineStatus ?? "—" }}</span>
                  <span class="mx-1 text-slate-400">&rarr;</span>
                  <span
                    :class="
                      row.replayStatus && row.replayStatus >= 200 && row.replayStatus < 300
                        ? 'font-semibold text-rose-600'
                        : 'text-slate-600'
                    "
                  >
                    {{ row.replayStatus ?? "—" }}
                  </span>
                </td>
                <td class="w-36 px-3 py-2.5">
                  <div v-if="row.similarity !== null" class="flex items-center gap-2">
                    <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                      <div
                        class="h-full rounded-full"
                        :class="
                          row.similarity >= 0.85
                            ? 'bg-rose-500'
                            : row.similarity >= 0.75
                              ? 'bg-amber-500'
                              : 'bg-emerald-500'
                        "
                        :style="{ width: `${Math.round(row.similarity * 100)}%` }"
                      />
                    </div>
                    <span class="w-10 text-right font-mono tabular-nums text-slate-600">
                      {{ Math.round(row.similarity * 100) }}%
                    </span>
                  </div>
                  <span v-else class="text-slate-400">—</span>
                </td>
                <td class="whitespace-nowrap px-4 py-2.5 text-right">
                  <button
                    type="button"
                    class="rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 transition hover:border-amber-300 hover:bg-amber-50 hover:text-amber-800"
                    @click="openBurpForSingle(row)"
                  >
                    Burp
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 3. Security Header Coverage Matrix -->
      <div class="card overflow-hidden">
        <div class="card-header">
          <div class="flex items-center gap-2.5">
            <ShieldCheck class="h-4 w-4 text-indigo-600" />
            <div>
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("sec.headersTitle") }}
              </h3>
              <p class="text-xs text-slate-500">
                {{ t("sec.headersDesc") }}
              </p>
            </div>
          </div>
          <span class="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600">
            {{ t("sec.hostsCount", { count: hostHeaderRows.length }) }}
          </span>
        </div>

        <div v-if="hostHeaderRows.length === 0" class="px-5 py-8 text-center text-xs text-slate-500">
          {{ t("sec.headersEmpty") }}
        </div>

        <div v-else class="overflow-x-auto">
          <table class="min-w-full divide-y divide-slate-200 text-xs">
            <thead class="table-head">
              <tr>
                <th class="px-4 py-2.5">{{ t("sec.colHost") }}</th>
                <th class="px-3 py-2.5 text-center">HSTS</th>
                <th class="px-3 py-2.5 text-center">CSP</th>
                <th class="px-3 py-2.5 text-center">XFO</th>
                <th class="px-3 py-2.5 text-center">XCTO</th>
                <th class="px-3 py-2.5 text-center">Referrer-Policy</th>
                <th class="px-4 py-2.5 text-center">{{ t("sec.colCorsWildcard") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 bg-white">
              <tr v-for="row in hostHeaderRows" :key="row.host" class="hover:bg-slate-50/70">
                <td class="px-4 py-3">
                  <p class="font-mono font-medium text-slate-800">{{ row.host }}</p>
                  <p class="mt-0.5 text-[11px] text-slate-400">
                    {{ t("sec.responsesCount", { count: row.seen }) }}
                    <span v-if="row.servers.length"> · {{ row.servers.join(", ") }}</span>
                  </p>
                </td>
                <td
                  v-for="(cell, idx) in [row.hsts, row.csp, row.xfo, row.xcto, row.referrer]"
                  :key="idx"
                  class="px-3 py-3 text-center"
                >
                  <span
                    class="inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset"
                    :class="pillClassForHeader(cell)"
                  >
                    {{ pillLabelForHeader(cell) }}
                  </span>
                </td>
                <td class="px-4 py-3 text-center">
                  <span
                    v-if="row.corsDangerous > 0"
                    class="inline-flex rounded-full bg-rose-50 px-2 py-0.5 text-[11px] font-medium text-rose-700 ring-1 ring-inset ring-rose-600/20"
                  >
                    {{ t("sec.corsDangerous", { count: row.corsDangerous }) }}
                  </span>
                  <span
                    v-else-if="row.corsWildcard > 0"
                    class="inline-flex rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800 ring-1 ring-inset ring-amber-600/20"
                  >
                    {{ t("sec.corsWildcardCount", { count: row.corsWildcard }) }}
                  </span>
                  <span
                    v-else
                    class="inline-flex rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 ring-1 ring-inset ring-emerald-600/20"
                  >
                    {{ t("sec.corsStrict") }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 4. Set-Cookie Audit & Redacted Leak Signals -->
      <div class="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <!-- Set-Cookie Audit -->
        <div class="card flex flex-col overflow-hidden">
          <div class="card-header">
            <div class="flex items-center gap-2">
              <Cookie class="h-4 w-4 text-amber-600" />
              <h3 class="text-sm font-semibold text-slate-900">{{ t("sec.cookieTitle") }}</h3>
            </div>
            <span class="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600">
              {{ cookieIssues.length }}
            </span>
          </div>
          <div v-if="cookieIssues.length === 0" class="px-5 py-8 text-center text-xs text-slate-500">
            {{ t("sec.cookieEmpty") }}
          </div>
          <div v-else class="max-h-72 divide-y divide-slate-100 overflow-y-auto">
            <div
              v-for="(ck, idx) in cookieIssues"
              :key="`${ck.host}-${ck.cookieName}-${idx}`"
              class="flex items-start justify-between gap-3 px-5 py-3 text-xs"
            >
              <div class="min-w-0">
                <p class="font-mono font-semibold text-slate-800">{{ ck.cookieName }}</p>
                <p class="mt-0.5 truncate text-[11px] text-slate-500">{{ ck.endpoint || ck.host }}</p>
                <div class="mt-1.5 flex flex-wrap gap-1">
                  <span
                    v-for="flag in ck.missingFlags"
                    :key="flag"
                    class="rounded bg-rose-50 px-1.5 py-0.5 font-mono text-[10px] font-medium text-rose-700 ring-1 ring-rose-200"
                  >
                    {{ t("sec.missingFlag", { flag }) }}
                  </span>
                </div>
              </div>
              <StatusBadge :status="ck.severity" />
            </div>
          </div>
        </div>

        <!-- Redacted Leak Signals -->
        <div class="card flex flex-col overflow-hidden">
          <div class="card-header">
            <div class="flex items-center gap-2">
              <KeyRound class="h-4 w-4 text-rose-600" />
              <h3 class="text-sm font-semibold text-slate-900">{{ t("sec.leaksTitle") }}</h3>
            </div>
            <span class="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600">
              {{ leakSignals.length }}
            </span>
          </div>
          <div v-if="leakSignals.length === 0" class="px-5 py-8 text-center text-xs text-slate-500">
            {{ t("sec.leaksEmpty") }}
          </div>
          <div v-else class="max-h-72 divide-y divide-slate-100 overflow-y-auto">
            <div
              v-for="(leak, idx) in leakSignals"
              :key="`${leak.kind}-${leak.source}-${idx}`"
              class="flex items-start justify-between gap-3 px-5 py-3 text-xs"
            >
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-slate-700">
                    {{ leak.kind }}
                  </span>
                  <span class="truncate text-slate-600" :title="leak.source">{{ leak.source }}</span>
                </div>
                <p class="mt-1.5 inline-block rounded bg-slate-900 px-2 py-0.5 font-mono text-[11px] text-amber-300">
                  {{ leak.sample }}
                </p>
                <p
                  v-if="leak.context"
                  data-testid="leak-context-snippet"
                  class="mt-1.5 rounded bg-slate-900/5 px-2.5 py-1.5 font-mono text-[11px] leading-relaxed break-all text-slate-700 ring-1 ring-inset ring-slate-200/80"
                >
                  {{ leak.context }}
                </p>
              </div>
              <StatusBadge :status="leak.severity" />
            </div>
          </div>
        </div>
      </div>

      <!-- 5. Nuclei Findings Section (if present) -->
      <div v-if="nucleiRows.length > 0" class="card overflow-hidden">
        <div class="card-header">
          <div class="flex items-center gap-2">
            <Radar class="h-4 w-4 text-indigo-600" />
            <h3 class="text-sm font-semibold text-slate-900">{{ t("sec.nucleiTitle") }}</h3>
          </div>
          <span class="rounded-full bg-indigo-50 px-2.5 py-0.5 text-xs font-medium text-indigo-700">
            {{ t("sec.nucleiMatched", { count: nucleiRows.length }) }}
          </span>
        </div>
        <div class="overflow-x-auto">
          <table class="min-w-full divide-y divide-slate-200 text-xs">
            <thead class="table-head">
              <tr>
                <th class="px-4 py-2.5">{{ t("sec.colTemplateId") }}</th>
                <th class="px-3 py-2.5">{{ t("sec.colName") }}</th>
                <th class="px-3 py-2.5">{{ t("sec.colMatchedAt") }}</th>
                <th class="px-3 py-2.5">{{ t("sec.colTags") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("sec.colSeverity") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 bg-white">
              <tr v-for="(n, idx) in nucleiRows" :key="`${n.templateId}-${idx}`" class="hover:bg-slate-50/70">
                <td class="whitespace-nowrap px-4 py-2.5 font-mono font-medium text-indigo-700">
                  {{ n.templateId }}
                </td>
                <td class="px-3 py-2.5 text-slate-800">{{ n.name }}</td>
                <td class="max-w-xs truncate px-3 py-2.5 font-mono text-slate-600" :title="n.matchedAt">
                  {{ n.matchedAt }}
                </td>
                <td class="px-3 py-2.5">
                  <div class="flex flex-wrap gap-1">
                    <span
                      v-for="tag in n.tags.slice(0, 4)"
                      :key="tag"
                      class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600"
                    >
                      {{ tag }}
                    </span>
                  </div>
                </td>
                <td class="px-4 py-2.5 text-right">
                  <StatusBadge :status="n.severity" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 6. Filterable Security Findings Table -->
      <div class="card overflow-hidden">
        <div class="card-header flex-wrap">
          <div>
            <h3 class="text-sm font-semibold text-slate-900">{{ t("sec.findingsTitle") }}</h3>
            <p class="text-xs text-slate-500">
              {{ t("sec.findingsCount", { shown: filteredFindings.length, total: allSecurityFindings.length }) }}
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <div class="relative">
              <Search class="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
              <input
                v-model="findingSearch"
                type="text"
                :placeholder="t('sec.searchFindings')"
                class="input-field py-1 pl-8 pr-3 text-xs"
              />
            </div>
            <select v-model="categoryFilter" class="input-field w-auto py-1 text-xs">
              <option value="all">{{ t("sec.allCategories") }}</option>
              <option v-for="cat in findingCategories" :key="cat" :value="cat">
                {{ cat }}
              </option>
            </select>
            <select v-model="severityFilter" class="input-field w-auto py-1 text-xs">
              <option value="all">{{ t("sec.allSeverities") }}</option>
              <option value="critical">{{ t("sec.sevCritical") }}</option>
              <option value="high">{{ t("sec.sevHigh") }}</option>
              <option value="medium">{{ t("sec.sevMedium") }}</option>
              <option value="low">{{ t("sec.sevLow") }}</option>
              <option value="info">{{ t("sec.sevInfo") }}</option>
            </select>
          </div>
        </div>

        <div v-if="filteredFindings.length === 0" class="px-5 py-10 text-center text-xs text-slate-500">
          {{ t("sec.findingsNoMatch") }}
        </div>

        <div v-else class="overflow-x-auto">
          <table class="min-w-full divide-y divide-slate-200 text-xs">
            <thead class="table-head">
              <tr>
                <th class="w-8 px-3 py-2.5" />
                <th class="px-3 py-2.5">{{ t("sec.colSeverity") }}</th>
                <th class="px-3 py-2.5">{{ t("sec.colCategory") }}</th>
                <th class="px-3 py-2.5">{{ t("sec.colTitle") }}</th>
                <th class="px-4 py-2.5">{{ t("sec.colEndpoint") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 bg-white">
              <template v-for="f in filteredFindings" :key="f.id">
                <tr class="cursor-pointer hover:bg-slate-50/70" @click="toggleFindingExpand(f.id)">
                  <td class="px-3 py-2.5 text-slate-400">
                    <ChevronDown v-if="expandedFindingIds.has(f.id)" class="h-3.5 w-3.5" />
                    <ChevronRight v-else class="h-3.5 w-3.5" />
                  </td>
                  <td class="whitespace-nowrap px-3 py-2.5">
                    <StatusBadge :status="f.severity" />
                  </td>
                  <td class="whitespace-nowrap px-3 py-2.5">
                    <span class="rounded bg-slate-100 px-2 py-0.5 font-mono text-[11px] text-slate-700">
                      {{ f.category }}
                    </span>
                  </td>
                  <td class="px-3 py-2.5 font-medium text-slate-800">{{ f.title }}</td>
                  <td class="max-w-xs truncate px-4 py-2.5 font-mono text-slate-600" :title="f.endpoint">
                    {{ f.endpoint }}
                  </td>
                </tr>
                <tr v-if="expandedFindingIds.has(f.id)" class="bg-slate-50/70">
                  <td colspan="5" class="space-y-2 px-6 py-3">
                    <div
                      v-if="typeof f.evidence?.context === 'string' && f.evidence.context"
                      data-testid="finding-context-snippet"
                      class="rounded-lg border border-amber-200/80 bg-amber-50/70 px-3 py-2 font-mono text-[11px] leading-relaxed break-all text-slate-800"
                    >
                      {{ f.evidence.context }}
                    </div>
                    <pre
                      class="max-h-48 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200"
                    >{{ JSON.stringify(f.evidence, null, 2) }}</pre>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- Burp Export Drawer -->
    <BurpExportDrawer
      :run-id="runId"
      :endpoints="burpTargetEndpoints"
      :open="burpDrawerOpen"
      @close="burpDrawerOpen = false"
    />
  </div>
</template>
