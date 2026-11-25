<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import {
  AlertTriangle,
  Boxes,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Download,
  ExternalLink,
  FileText,
  GitCommitHorizontal,
  Loader2,
  Play,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-vue-next";
import { getProjectCapabilities, promoteCapabilityToScenario } from "@/api/projects";
import { downloadAcceptanceReport, downloadPostmanCollection } from "@/api/reports";
import type {
  BusinessCapability,
  CapabilityEndpointRef,
  CapabilitySlotName,
  CapabilitySlotStatus,
  ProjectCapabilityMapResponse,
  ScenarioItem,
  ScenarioPriority,
} from "@/api/types";
import EmptyState from "@/components/layout/EmptyState.vue";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import TryItDrawer, { type TryItEndpointInput } from "@/components/runs/TryItDrawer.vue";
import { useI18n } from "@/i18n";
import "@/i18n/locales/capabilities";
import { useWorkspace } from "@/stores/workspace";

type ScopeMode = "core" | "live" | "all";

const route = useRoute();
const { locale, t } = useI18n();
const { activeProjectId, ensureLoaded } = useWorkspace();

const projectId = computed(() => {
  const paramPid = typeof route.params.projectId === "string" ? route.params.projectId : "";
  return paramPid || activeProjectId.value || "";
});

const DEFAULT_ROLES = ["admin", "pharmacy", "doctor", "assistant", "patient"] as const;

const SLOT_ORDER: CapabilitySlotName[] = [
  "happy_path",
  "invalid_params",
  "illegal_state",
  "authz_role",
  "consistency_idempotency",
];

const loading = ref(true);
const error = ref<string | null>(null);
const data = ref<ProjectCapabilityMapResponse | null>(null);

// Filters: Core Only ON by default for 3-second readability
const scopeMode = ref<ScopeMode>("core");
const searchQuery = ref("");
const selectedRole = ref<string>("ALL");
const selectedModule = ref<string>("ALL");

// UI state
const expandedKeys = ref<Record<string, boolean>>({});
const promotingKey = ref<string | null>(null);
const promoteError = ref<string | null>(null);
const promotedBanner = ref<{
  capabilityName: string;
  scenario: ScenarioItem;
} | null>(null);

// Inline Try It Drawer
const tryItOpen = ref(false);
const tryItEndpoint = ref<TryItEndpointInput | null>(null);
const exportingKind = ref<"html" | "markdown" | "postman" | null>(null);

async function handleExport(kind: "html" | "markdown" | "postman"): Promise<void> {
  if (!projectId.value || exportingKind.value) return;
  exportingKind.value = kind;
  error.value = null;
  try {
    if (kind === "postman") {
      await downloadPostmanCollection(projectId.value);
    } else {
      await downloadAcceptanceReport(projectId.value, { format: kind });
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("cap.errorExport");
  } finally {
    exportingKind.value = null;
  }
}

let loadSeq = 0;
let searchDebounceTimer: number | null = null;

const metrics = computed(() => data.value?.metrics ?? null);
const modules = computed(() => data.value?.modules ?? []);
const capabilities = computed(() => data.value?.capabilities ?? []);

const coreCount = computed(
  () => metrics.value?.core_capabilities ?? metrics.value?.live_capabilities ?? 0
);

const slotCounts = computed(() => {
  const m = metrics.value;
  if (
    m &&
    typeof m.slots_passed === "number" &&
    typeof m.slots_failed === "number" &&
    typeof m.slots_pending === "number"
  ) {
    return {
      passed: m.slots_passed,
      failed: m.slots_failed,
      pending: m.slots_pending,
      missing: m.slots_missing ?? 0,
    };
  }
  let passed = 0;
  let failed = 0;
  let pending = 0;
  let missing = 0;
  for (const cap of capabilities.value) {
    for (const s of cap.case_matrix ?? []) {
      if (s.status === "covered") passed += 1;
      else if (s.status === "failed") failed += 1;
      else if (s.status === "pending") pending += 1;
      else missing += 1;
    }
  }
  return { passed, failed, pending, missing };
});

const roleOptions = computed<string[]>(() => {
  const seen = new Set<string>(DEFAULT_ROLES);
  for (const cap of capabilities.value) {
    for (const r of cap.roles) {
      if (r && r !== "common") seen.add(r);
    }
  }
  return Array.from(seen);
});

const hasActiveFilters = computed(
  () =>
    scopeMode.value !== "all" ||
    searchQuery.value.trim().length > 0 ||
    selectedRole.value !== "ALL" ||
    selectedModule.value !== "ALL"
);

async function fetchCapabilities(silent = false): Promise<void> {
  const pid = projectId.value;
  const reqId = ++loadSeq;
  if (!pid) {
    data.value = null;
    loading.value = false;
    return;
  }

  if (!silent) {
    loading.value = true;
  }
  error.value = null;

  try {
    const res = await getProjectCapabilities(pid, {
      module: selectedModule.value !== "ALL" ? selectedModule.value : undefined,
      role: selectedRole.value !== "ALL" ? selectedRole.value : undefined,
      core_only: scopeMode.value === "core",
      live_only: scopeMode.value === "live",
      q: searchQuery.value.trim() || undefined,
    });
    if (reqId !== loadSeq || projectId.value !== pid) return;
    data.value = res;
  } catch (err) {
    if (reqId !== loadSeq || projectId.value !== pid) return;
    error.value = err instanceof Error ? err.message : t("cap.errorLoad");
  } finally {
    if (reqId === loadSeq) {
      loading.value = false;
    }
  }
}

function resetFilters(): void {
  scopeMode.value = "all";
  searchQuery.value = "";
  selectedRole.value = "ALL";
  selectedModule.value = "ALL";
}

function toggleExpand(key: string): void {
  expandedKeys.value = {
    ...expandedKeys.value,
    [key]: !expandedKeys.value[key],
  };
}

function openTryIt(ep: CapabilityEndpointRef): void {
  tryItEndpoint.value = {
    method: ep.method,
    path: ep.path,
    host: ep.host || undefined,
    summary: `${roleLabel(ep.role)} · ${ep.action}`,
    customHeaders: ep.custom_headers,
  };
  tryItOpen.value = true;
}

function capabilityTitle(cap: BusinessCapability): string {
  if (locale.value === "zh") {
    return cap.title_zh || cap.display_name || cap.key;
  }
  return cap.title_en || cap.display_name || cap.key;
}

async function handlePromote(cap: BusinessCapability): Promise<void> {
  const pid = projectId.value;
  if (!pid || promotingKey.value) return;

  promotingKey.value = cap.key;
  promoteError.value = null;
  try {
    const created = await promoteCapabilityToScenario(pid, {
      capability_key: cap.key,
      priority: cap.recommended_priority,
      lang: locale.value === "en" ? "en" : "zh",
    });
    if (projectId.value !== pid) return;
    promotedBanner.value = {
      capabilityName: capabilityTitle(cap),
      scenario: created,
    };
    await fetchCapabilities(true);
  } catch (err) {
    if (projectId.value !== pid) return;
    promoteError.value = err instanceof Error ? err.message : t("cap.errorPromote");
  } finally {
    if (promotingKey.value === cap.key) {
      promotingKey.value = null;
    }
  }
}

function priorityBadgeClass(priority: ScenarioPriority): string {
  switch (priority) {
    case "P0":
      return "border-rose-200 bg-rose-50 text-rose-700";
    case "P1":
      return "border-amber-200 bg-amber-50 text-amber-800";
    case "P2":
      return "border-sky-200 bg-sky-50 text-sky-700";
    default:
      return "border-slate-200 bg-slate-100 text-slate-600";
  }
}

function roleBadgeClass(role: string): string {
  switch (role) {
    case "admin":
      return "border-indigo-200 bg-indigo-50 text-indigo-700";
    case "pharmacy":
      return "border-teal-200 bg-teal-50 text-teal-700";
    case "doctor":
      return "border-sky-200 bg-sky-50 text-sky-700";
    case "assistant":
      return "border-violet-200 bg-violet-50 text-violet-700";
    case "patient":
      return "border-emerald-200 bg-emerald-50 text-emerald-700";
    default:
      return "border-slate-200 bg-slate-50 text-slate-600";
  }
}

function roleLabel(role: string): string {
  const known = new Set(["admin", "pharmacy", "doctor", "assistant", "patient", "common"]);
  return known.has(role) ? t(`cap.role.${role}`) : role;
}

function getSlot(cap: BusinessCapability, slot: CapabilitySlotName): CapabilitySlotStatus {
  const found = (cap.case_matrix ?? []).find((s) => s.slot === slot);
  return found ?? { slot, status: "missing", count: 0 };
}

function slotTooltip(slotItem: CapabilitySlotStatus): string {
  const name = t(`cap.slot.${slotItem.slot}`);
  const status = t(`cap.slotStatus.${slotItem.status}`);
  return slotItem.count > 0 ? `${name} · ${status} (${slotItem.count})` : `${name} · ${status}`;
}

function compactStateSummary(observed: Record<string, string[]>): string {
  const entries = Object.entries(observed || {}).filter(
    ([, vals]) => Array.isArray(vals) && vals.length > 0
  );
  if (entries.length === 0) return "";
  const [, vals] = entries[0];
  const shown = vals.slice(0, 5).join(",");
  return vals.length > 5 ? `${shown}…` : shown;
}

function formatStateEntries(observed: Record<string, string[]>): Array<[string, string]> {
  return Object.entries(observed || {})
    .filter(([, vals]) => Array.isArray(vals) && vals.length > 0)
    .slice(0, 6)
    .map(([field, vals]) => [field, vals.slice(0, 10).join(", ") + (vals.length > 10 ? "…" : "")]);
}

function methodBadgeClass(method: string): string {
  switch (method.toUpperCase()) {
    case "GET":
      return "border-emerald-200 bg-emerald-50 text-emerald-700";
    case "POST":
      return "border-indigo-200 bg-indigo-50 text-indigo-700";
    case "PUT":
    case "PATCH":
      return "border-amber-200 bg-amber-50 text-amber-800";
    case "DELETE":
      return "border-rose-200 bg-rose-50 text-rose-700";
    default:
      return "border-slate-200 bg-slate-100 text-slate-700";
  }
}

function formatPct(val: number | undefined): string {
  return `${Math.round(Number(val ?? 0))}%`;
}

watch(projectId, () => {
  expandedKeys.value = {};
  promotedBanner.value = null;
  promoteError.value = null;
  scopeMode.value = "core";
  selectedModule.value = "ALL";
  selectedRole.value = "ALL";
  searchQuery.value = "";
  void fetchCapabilities();
});

watch([scopeMode, selectedRole, selectedModule], () => {
  void fetchCapabilities();
});

watch(searchQuery, () => {
  if (searchDebounceTimer !== null) {
    window.clearTimeout(searchDebounceTimer);
  }
  searchDebounceTimer = window.setTimeout(() => {
    void fetchCapabilities(true);
  }, 220);
});

onMounted(async () => {
  await ensureLoaded();
  void fetchCapabilities();
});

onBeforeUnmount(() => {
  if (searchDebounceTimer !== null) {
    window.clearTimeout(searchDebounceTimer);
  }
});
</script>

<template>
  <div class="space-y-4 pb-8">
    <!-- Top Deliverable Exports Bar -->
    <div v-if="projectId && !loading" class="flex flex-wrap items-center justify-end gap-2">
      <button
        type="button"
        data-testid="cap-export-html-btn"
        class="btn-secondary !py-1.5 !text-xs"
        :disabled="Boolean(exportingKind)"
        @click="handleExport('html')"
      >
        <Loader2 v-if="exportingKind === 'html'" class="h-3.5 w-3.5 animate-spin" />
        <FileText v-else class="h-3.5 w-3.5" />
        <span>{{ exportingKind === "html" ? t("cap.exporting") : t("cap.exportHtml") }}</span>
      </button>
      <button
        type="button"
        data-testid="cap-export-md-btn"
        class="btn-secondary !py-1.5 !text-xs"
        :disabled="Boolean(exportingKind)"
        @click="handleExport('markdown')"
      >
        <Loader2 v-if="exportingKind === 'markdown'" class="h-3.5 w-3.5 animate-spin" />
        <FileText v-else class="h-3.5 w-3.5" />
        <span>{{ exportingKind === "markdown" ? t("cap.exporting") : t("cap.exportMd") }}</span>
      </button>
      <button
        type="button"
        data-testid="cap-export-postman-btn"
        class="btn-secondary !py-1.5 !text-xs"
        :disabled="Boolean(exportingKind)"
        @click="handleExport('postman')"
      >
        <Loader2 v-if="exportingKind === 'postman'" class="h-3.5 w-3.5 animate-spin" />
        <Download v-else class="h-3.5 w-3.5" />
        <span>
          {{ exportingKind === "postman" ? t("cap.exporting") : t("cap.exportPostman") }}
        </span>
      </button>
    </div>

    <!-- 1. Clear 1-Line Purpose + Visual Legend Bar -->
    <section
      v-if="metrics"
      data-testid="cap-purpose-legend"
      class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-5 py-3.5 shadow-sm"
    >
      <p class="text-xs leading-relaxed font-medium text-slate-700">
        {{
          t("cap.purposeLine", {
            endpoints: metrics.total_endpoints,
            core: coreCount,
          })
        }}
      </p>

      <div class="flex flex-wrap items-center gap-2 text-[11px] font-medium">
        <span class="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-emerald-800">
          <span>✅</span>
          <span>{{ t("cap.legend.passed") }}</span>
        </span>
        <span class="inline-flex items-center gap-1 rounded-full border border-rose-200 bg-rose-50 px-2.5 py-0.5 text-rose-700">
          <span>❌</span>
          <span>{{ t("cap.legend.failed") }}</span>
        </span>
        <span class="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-0.5 text-amber-800">
          <span>⏳</span>
          <span>{{ t("cap.legend.pending") }}</span>
        </span>
        <span class="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-slate-500">
          <span>—</span>
          <span>{{ t("cap.legend.missing") }}</span>
        </span>
      </div>
    </section>

    <!-- 2. Clean 3-Card Summary Strip -->
    <section
      v-if="metrics"
      data-testid="cap-kpi-strip"
      class="grid grid-cols-1 gap-3.5 md:grid-cols-3"
    >
      <!-- Card 1: Core Business Capabilities -->
      <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-slate-500">
            {{ t("cap.summary.coreTitle") }}
          </span>
          <Boxes class="h-4 w-4 text-indigo-600" />
        </div>
        <div class="mt-2 flex items-baseline gap-1.5">
          <span class="font-mono text-2xl font-bold text-slate-900">{{ coreCount }}</span>
          <span v-if="t('cap.summary.coreUnit')" class="text-xs font-medium text-slate-500">
            {{ t("cap.summary.coreUnit") }}
          </span>
        </div>
        <p class="mt-1.5 text-xs text-slate-500">
          {{
            t("cap.summary.coreSub", {
              endpoints: metrics.total_endpoints,
              stateful: metrics.stateful_capabilities,
            })
          }}
        </p>
      </div>

      <!-- Card 2: 5-Defense Live Verification Pulse -->
      <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-slate-500">
            {{ t("cap.summary.matrixTitle") }}
          </span>
          <GitCommitHorizontal class="h-4 w-4 text-violet-600" />
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-2 font-mono text-sm font-bold">
          <span class="inline-flex items-center gap-1 rounded-md bg-emerald-50 px-2 py-0.5 text-emerald-700">
            <span>✅</span>
            <span>{{ t("cap.summary.matrixPassed", { count: slotCounts.passed }) }}</span>
          </span>
          <span
            class="inline-flex items-center gap-1 rounded-md px-2 py-0.5"
            :class="slotCounts.failed > 0 ? 'bg-rose-50 text-rose-700' : 'bg-slate-50 text-slate-500'"
          >
            <span>❌</span>
            <span>{{ t("cap.summary.matrixFailed", { count: slotCounts.failed }) }}</span>
          </span>
          <span class="inline-flex items-center gap-1 rounded-md bg-amber-50 px-2 py-0.5 text-amber-800">
            <span>⏳</span>
            <span>{{ t("cap.summary.matrixPending", { count: slotCounts.pending }) }}</span>
          </span>
        </div>
        <p class="mt-1.5 text-xs text-slate-500">
          {{ t("cap.summary.matrixSub") }}
        </p>
      </div>

      <!-- Card 3: Invariant Assertion Pass Rate -->
      <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-slate-500">
            {{ t("cap.summary.invariantTitle") }}
          </span>
          <ShieldCheck class="h-4 w-4 text-emerald-600" />
        </div>
        <div class="mt-2 flex items-baseline gap-2">
          <span class="font-mono text-2xl font-bold text-slate-900">
            {{ formatPct(metrics.invariant_pass_rate_pct) }}
          </span>
        </div>
        <p class="mt-1.5 text-xs text-slate-500">
          {{
            t("cap.summary.invariantSub", {
              passed: metrics.invariant_assertions_passed,
              total: metrics.invariant_assertions_total,
            })
          }}
        </p>
      </div>
    </section>

    <!-- Success Toast for Promoted 5-Case Scenario -->
    <div
      v-if="promotedBanner"
      data-testid="cap-promote-toast"
      class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs text-emerald-900 shadow-sm"
    >
      <div class="flex items-center gap-2">
        <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-600" />
        <span class="font-medium">
          {{
            t("cap.promotedToast", {
              capability: promotedBanner.capabilityName,
              scenario: promotedBanner.scenario.name,
            })
          }}
        </span>
      </div>
      <div class="flex items-center gap-2">
        <RouterLink
          :to="{
            path: `/p/${encodeURIComponent(projectId)}/scenarios`,
            query: { id: promotedBanner.scenario.id },
          }"
          data-testid="cap-open-promoted-scenario"
          class="inline-flex items-center gap-1 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-emerald-700"
        >
          <span>{{ t("cap.openScenario") }}</span>
          <ExternalLink class="h-3.5 w-3.5" />
        </RouterLink>
        <button
          type="button"
          class="rounded-md p-1 text-emerald-700 hover:bg-emerald-100"
          @click="promotedBanner = null"
        >
          <X class="h-3.5 w-3.5" />
        </button>
      </div>
    </div>

    <!-- Error Notice -->
    <div
      v-if="error || promoteError"
      class="flex items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-800"
    >
      <div class="flex items-center gap-2">
        <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
        <span>{{ error || promoteError }}</span>
      </div>
      <button
        type="button"
        class="rounded-md p-1 text-rose-600 hover:bg-rose-100"
        @click="
          error = null;
          promoteError = null;
        "
      >
        <X class="h-3.5 w-3.5" />
      </button>
    </div>

    <!-- 3. Clean Filter Bar -->
    <section class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div class="flex flex-wrap items-center gap-2.5">
        <!-- Search -->
        <div class="relative min-w-[220px] flex-1">
          <Search
            class="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-slate-400"
          />
          <input
            v-model="searchQuery"
            type="text"
            data-testid="cap-search-input"
            :placeholder="t('cap.searchPlaceholder')"
            class="w-full rounded-lg border border-slate-300 bg-white py-1.5 pr-3 pl-9 text-xs text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 focus:outline-none"
          />
        </div>

        <!-- Compact Segmented Scope Toggle: Core Only (default) | All Live | All -->
        <div
          data-testid="cap-scope-toggle"
          class="inline-flex rounded-lg border border-slate-200 bg-slate-100 p-0.5 text-xs font-medium"
        >
          <button
            type="button"
            data-testid="cap-scope-core"
            class="rounded-md px-2.5 py-1 transition"
            :class="
              scopeMode === 'core'
                ? 'bg-white text-indigo-700 shadow-xs'
                : 'text-slate-600 hover:text-slate-900'
            "
            @click="scopeMode = 'core'"
          >
            {{ t("cap.scope.core") }}
          </button>
          <button
            type="button"
            data-testid="cap-scope-live"
            class="rounded-md px-2.5 py-1 transition"
            :class="
              scopeMode === 'live'
                ? 'bg-white text-indigo-700 shadow-xs'
                : 'text-slate-600 hover:text-slate-900'
            "
            @click="scopeMode = 'live'"
          >
            {{ t("cap.scope.live") }}
          </button>
          <button
            type="button"
            data-testid="cap-scope-all"
            class="rounded-md px-2.5 py-1 transition"
            :class="
              scopeMode === 'all'
                ? 'bg-white text-indigo-700 shadow-xs'
                : 'text-slate-600 hover:text-slate-900'
            "
            @click="scopeMode = 'all'"
          >
            {{ t("cap.scope.all") }}
          </button>
        </div>

        <!-- Role Filter Dropdown -->
        <select
          v-model="selectedRole"
          data-testid="cap-role-select"
          :aria-label="t('cap.filter.roleLabel')"
          class="rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 focus:outline-none"
        >
          <option value="ALL">{{ t("cap.filter.allRoles") }}</option>
          <option v-for="r in roleOptions" :key="r" :value="r">
            {{ roleLabel(r) }}
          </option>
        </select>

        <!-- Refresh -->
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
          :disabled="loading"
          @click="fetchCapabilities()"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="{ 'animate-spin': loading }" />
          <span>{{ t("cap.refresh") }}</span>
        </button>
      </div>

      <!-- Clean Business Module Pills -->
      <div
        v-if="modules.length > 0"
        data-testid="cap-module-bar"
        class="mt-3 flex flex-wrap items-center gap-1.5 border-t border-slate-100 pt-3"
      >
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition"
          :class="
            selectedModule === 'ALL'
              ? 'border-indigo-600 bg-indigo-600 text-white'
              : 'border-slate-200 bg-slate-50 text-slate-700 hover:border-indigo-300 hover:bg-indigo-50/50'
          "
          @click="selectedModule = 'ALL'"
        >
          <span>{{ t("cap.filter.allModules") }}</span>
          <span
            class="rounded-full px-1.5 py-0.2 font-mono text-[10px]"
            :class="selectedModule === 'ALL' ? 'bg-white/20 text-white' : 'bg-white text-slate-600'"
          >
            {{ metrics?.total_capabilities ?? 0 }}
          </span>
        </button>

        <button
          v-for="mod in modules"
          :key="mod.module"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition"
          :class="
            selectedModule === mod.module
              ? 'border-indigo-600 bg-indigo-600 text-white'
              : 'border-slate-200 bg-white text-slate-700 hover:border-indigo-300 hover:bg-indigo-50/50'
          "
          @click="selectedModule = mod.module"
        >
          <span class="font-mono">{{ mod.module }}</span>
          <span
            class="rounded-full px-1.5 py-0.2 font-mono text-[10px]"
            :class="
              selectedModule === mod.module ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-600'
            "
          >
            {{ mod.live_capabilities }}/{{ mod.capabilities }}
          </span>
        </button>

        <span class="ml-auto text-[11px] text-slate-400">
          {{ t("cap.filter.showing", { count: capabilities.length }) }}
        </span>
      </div>
    </section>

    <!-- Loading State -->
    <div
      v-if="loading && !data"
      class="flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500"
    >
      <Loader2 class="h-4 w-4 animate-spin text-indigo-600" />
      <span>{{ t("cap.loading") }}</span>
    </div>

    <!-- Empty State: No endpoints / capabilities at all -->
    <EmptyState
      v-else-if="metrics && metrics.total_capabilities === 0"
      :title="t('cap.emptyTitle')"
      :description="t('cap.emptyDesc')"
    />

    <!-- Empty State: Filtered down to 0 -->
    <div
      v-else-if="capabilities.length === 0"
      class="rounded-xl border border-dashed border-slate-300 bg-white px-6 py-12 text-center"
    >
      <p class="text-sm font-semibold text-slate-800">{{ t("cap.emptyFilteredTitle") }}</p>
      <p class="mt-1 text-xs text-slate-500">{{ t("cap.emptyFilteredDesc") }}</p>
      <button
        v-if="hasActiveFilters"
        type="button"
        class="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3.5 py-1.5 text-xs font-medium text-indigo-700 transition hover:bg-indigo-100"
        @click="resetFilters"
      >
        {{ t("cap.clearFilters") }}
      </button>
    </div>

    <!-- 4. Clean 5-Column Excel-Style QA Acceptance Matrix Table -->
    <section
      v-else
      class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm"
    >
      <div class="overflow-x-auto">
        <table
          data-testid="capability-table"
          class="min-w-full divide-y divide-slate-200 text-left text-xs"
        >
          <thead class="bg-slate-50 text-[11px] font-semibold tracking-wide text-slate-500 uppercase">
            <tr>
              <th class="min-w-[260px] px-4 py-3">{{ t("cap.table.colCapability") }}</th>
              <th class="min-w-[140px] px-3 py-3">{{ t("cap.table.colRoles") }}</th>
              <th
                v-for="slotName in SLOT_ORDER"
                :key="`hdr-${slotName}`"
                class="w-20 px-2 py-3 text-center"
              >
                {{ t(`cap.slot.${slotName}`) }}
              </th>
              <th class="w-36 px-4 py-3 text-right">{{ t("cap.table.colAction") }}</th>
            </tr>
          </thead>

          <tbody class="divide-y divide-slate-100 bg-white">
            <template v-for="cap in capabilities" :key="cap.key">
              <!-- Main Matrix Row -->
              <tr
                data-testid="capability-row"
                class="cursor-pointer transition hover:bg-indigo-50/35"
                :class="{ 'bg-slate-50/70': expandedKeys[cap.key] }"
                @click="toggleExpand(cap.key)"
              >
                <!-- Col 1: Capability Title + Priority + Compact Subline -->
                <td class="px-4 py-3 align-middle">
                  <div class="flex items-start gap-2">
                    <ChevronDown
                      v-if="expandedKeys[cap.key]"
                      class="mt-0.5 h-4 w-4 shrink-0 text-indigo-600"
                    />
                    <ChevronRight v-else class="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />

                    <div class="min-w-0 space-y-1">
                      <div class="flex flex-wrap items-center gap-1.5">
                        <span
                          class="rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold"
                          :class="priorityBadgeClass(cap.recommended_priority)"
                        >
                          {{ cap.recommended_priority }}
                        </span>
                        <span class="text-sm font-semibold text-slate-900">
                          {{ capabilityTitle(cap) }}
                        </span>
                        <span class="font-mono text-[11px] text-slate-400">
                          {{ cap.key }}
                        </span>
                      </div>

                      <div class="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                        <span class="font-mono">
                          {{
                            t("cap.rowSubline", {
                              live: cap.live_endpoints,
                              total: cap.total_endpoints,
                              writes: cap.write_endpoints,
                            })
                          }}
                        </span>
                        <span
                          v-if="cap.has_state_machine && compactStateSummary(cap.observed_statuses)"
                          class="inline-flex items-center gap-1 rounded bg-violet-50 px-1.5 py-0.5 font-mono text-[10px] font-medium text-violet-700"
                        >
                          <GitCommitHorizontal class="h-3 w-3" />
                          <span>
                            {{
                              t("cap.rowStatePill", {
                                vals: compactStateSummary(cap.observed_statuses),
                              })
                            }}
                          </span>
                        </span>
                      </div>
                    </div>
                  </div>
                </td>

                <!-- Col 2: Localized Role Pills -->
                <td class="px-3 py-3 align-middle">
                  <div class="flex flex-wrap gap-1">
                    <span
                      v-for="role in cap.roles"
                      :key="`${cap.key}-role-${role}`"
                      class="rounded border px-1.5 py-0.5 text-[11px] font-medium"
                      :class="roleBadgeClass(role)"
                    >
                      {{ roleLabel(role) }}
                    </span>
                  </div>
                </td>

                <!-- Cols 3-7: 5 Defense Line Centered Status Cells -->
                <td
                  v-for="slotName in SLOT_ORDER"
                  :key="`${cap.key}-cell-${slotName}`"
                  class="px-2 py-3 text-center align-middle"
                >
                  <template v-for="slotItem in [getSlot(cap, slotName)]" :key="slotItem.slot">
                    <span
                      v-if="slotItem.status === 'covered'"
                      :title="slotTooltip(slotItem)"
                      class="inline-flex items-center justify-center rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs font-semibold text-emerald-700"
                    >
                      ✅
                    </span>
                    <span
                      v-else-if="slotItem.status === 'failed'"
                      :title="slotTooltip(slotItem)"
                      class="inline-flex items-center justify-center gap-0.5 rounded-md border border-rose-200 bg-rose-50 px-2 py-1 text-[11px] font-bold text-rose-700"
                    >
                      <span>❌</span>
                      <span>{{ t("cap.cell.failed") }}</span>
                    </span>
                    <span
                      v-else-if="slotItem.status === 'pending'"
                      :title="slotTooltip(slotItem)"
                      class="inline-flex items-center justify-center gap-0.5 rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] font-medium text-amber-800"
                    >
                      <span>⏳</span>
                      <span>{{ t("cap.cell.pending") }}</span>
                    </span>
                    <span
                      v-else
                      :title="slotTooltip(slotItem)"
                      class="inline-flex items-center justify-center text-sm font-medium text-slate-300"
                    >
                      —
                    </span>
                  </template>
                </td>

                <!-- Col 8: Action (View Scenario + Sync icon OR Generate 5 Cases) -->
                <td class="px-4 py-3 text-right align-middle" @click.stop>
                  <div
                    v-if="cap.linked_scenarios.length > 0"
                    class="inline-flex items-center justify-end gap-1.5"
                  >
                    <RouterLink
                      :to="{
                        path: `/p/${encodeURIComponent(projectId)}/scenarios`,
                        query: { id: cap.linked_scenarios[0].id },
                      }"
                      data-testid="cap-linked-scenario-link"
                      class="inline-flex items-center gap-1 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs font-semibold text-emerald-800 transition hover:bg-emerald-100"
                    >
                      <span>{{ t("cap.viewScenarioBtn") }}</span>
                      <ExternalLink class="h-3 w-3" />
                    </RouterLink>
                    <button
                      type="button"
                      data-testid="cap-promote-btn"
                      :title="t('cap.syncBtnTitle')"
                      :disabled="promotingKey === cap.key"
                      class="inline-flex h-7 w-7 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-500 transition hover:border-indigo-300 hover:bg-indigo-50 hover:text-indigo-700 disabled:opacity-50"
                      @click="handlePromote(cap)"
                    >
                      <Loader2 v-if="promotingKey === cap.key" class="h-3.5 w-3.5 animate-spin" />
                      <RefreshCw v-else class="h-3.5 w-3.5" />
                    </button>
                  </div>

                  <button
                    v-else
                    type="button"
                    data-testid="cap-promote-btn"
                    :disabled="promotingKey === cap.key"
                    class="inline-flex items-center gap-1 rounded-lg border border-indigo-600 bg-indigo-600 px-2.5 py-1.5 text-xs font-semibold text-white shadow-xs transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
                    @click="handlePromote(cap)"
                  >
                    <Loader2 v-if="promotingKey === cap.key" class="h-3.5 w-3.5 animate-spin" />
                    <Sparkles v-else class="h-3.5 w-3.5" />
                    <span>
                      {{ promotingKey === cap.key ? t("cap.promoting") : t("cap.promoteBtn") }}
                    </span>
                  </button>
                </td>
              </tr>

              <!-- Expandable Row Detail: Actions, States, Linked Scenarios & Grouped Endpoints Table -->
              <tr v-if="expandedKeys[cap.key]" data-testid="cap-endpoints-drawer">
                <td colspan="8" class="border-t border-slate-200 bg-slate-50/75 px-6 py-4">
                  <div class="space-y-3">
                    <!-- Metadata Strip (Actions, Full State Enums, All Linked Scenarios) -->
                    <div class="flex flex-wrap items-center gap-x-5 gap-y-2 text-[11px]">
                      <div v-if="cap.actions.length > 0" class="flex flex-wrap items-center gap-1">
                        <span class="font-semibold text-slate-500">{{ t("cap.actionsLabel") }}:</span>
                        <span
                          v-for="action in cap.actions"
                          :key="`${cap.key}-act-${action}`"
                          class="rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[10px] font-medium text-slate-700"
                        >
                          {{ action }}
                        </span>
                      </div>

                      <div
                        v-if="formatStateEntries(cap.observed_statuses).length > 0"
                        class="flex flex-wrap items-center gap-1"
                      >
                        <span class="font-semibold text-slate-500">{{ t("cap.statesLabel") }}:</span>
                        <span
                          v-for="[field, vals] in formatStateEntries(cap.observed_statuses)"
                          :key="`${cap.key}-st-${field}`"
                          class="rounded border border-violet-200 bg-violet-50 px-1.5 py-0.5 font-mono text-[10px] font-medium text-violet-800"
                        >
                          {{ field }}: {{ vals }}
                        </span>
                      </div>

                      <div
                        v-if="cap.linked_scenarios.length > 0"
                        class="flex flex-wrap items-center gap-1"
                      >
                        <span class="font-semibold text-slate-500">
                          {{ t("cap.linkedScenariosLabel") }}:
                        </span>
                        <RouterLink
                          v-for="sc in cap.linked_scenarios"
                          :key="`${cap.key}-sc-${sc.id}`"
                          :to="{
                            path: `/p/${encodeURIComponent(projectId)}/scenarios`,
                            query: { id: sc.id },
                          }"
                          class="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-800 hover:bg-emerald-100"
                        >
                          <span>{{ sc.name }}</span>
                          <ExternalLink class="h-2.5 w-2.5" />
                        </RouterLink>
                      </div>
                    </div>

                    <!-- Grouped Endpoints Table with Inline Try It -->
                    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
                      <div class="border-b border-slate-100 bg-slate-50/70 px-4 py-2 text-[11px] font-semibold text-slate-600">
                        {{ t("cap.endpointsHeader", { count: cap.endpoints.length }) }}
                      </div>

                      <div
                        v-if="cap.endpoints.length === 0"
                        class="px-4 py-5 text-center text-xs text-slate-500"
                      >
                        {{ t("cap.noEndpoints") }}
                      </div>

                      <div v-else class="max-h-72 overflow-auto">
                        <table class="min-w-full divide-y divide-slate-100 text-left text-xs">
                          <thead class="sticky top-0 bg-white font-semibold text-slate-500 uppercase">
                            <tr>
                              <th class="px-4 py-2">{{ t("cap.col.method") }}</th>
                              <th class="px-3 py-2">{{ t("cap.col.path") }}</th>
                              <th class="px-3 py-2">{{ t("cap.col.source") }}</th>
                              <th class="px-3 py-2">{{ t("cap.col.role") }}</th>
                              <th class="px-3 py-2">{{ t("cap.col.action") }}</th>
                              <th class="px-4 py-2 text-right">{{ t("cap.col.replay") }}</th>
                            </tr>
                          </thead>
                          <tbody class="divide-y divide-slate-100">
                            <tr
                              v-for="(ep, idx) in cap.endpoints"
                              :key="`${cap.key}-ep-${idx}-${ep.method}-${ep.path}`"
                              class="hover:bg-slate-50/80"
                            >
                              <td class="px-4 py-2">
                                <span
                                  class="inline-flex rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold"
                                  :class="methodBadgeClass(ep.method)"
                                >
                                  {{ ep.method }}
                                </span>
                              </td>
                              <td class="px-3 py-2 font-mono text-slate-800">
                                <div class="break-all">{{ ep.path }}</div>
                                <div v-if="ep.host" class="text-[10px] text-slate-400">
                                  {{ ep.host }}
                                </div>
                              </td>
                              <td class="px-3 py-2">
                                <StatusBadge :status="ep.discovery_source" size="sm" />
                              </td>
                              <td class="px-3 py-2">
                                <span
                                  class="rounded border px-1.5 py-0.5 text-[10px] font-medium"
                                  :class="roleBadgeClass(ep.role)"
                                >
                                  {{ roleLabel(ep.role) }}
                                </span>
                              </td>
                              <td class="px-3 py-2 font-mono text-[11px] text-slate-600">
                                {{ ep.action }}
                              </td>
                              <td class="px-4 py-2 text-right">
                                <button
                                  type="button"
                                  data-testid="cap-endpoint-tryit-btn"
                                  class="inline-flex items-center gap-1 rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[11px] font-semibold text-indigo-700 transition hover:bg-indigo-100"
                                  @click="openTryIt(ep)"
                                >
                                  <Play class="h-3 w-3" />
                                  <span>{{ t("cap.tryIt") }}</span>
                                </button>
                              </td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>

    <!-- Inline Try It Replay Drawer -->
    <TryItDrawer
      :open="tryItOpen"
      :project-id="projectId"
      :endpoint="tryItEndpoint"
      @close="tryItOpen = false"
      @synced="fetchCapabilities(true)"
    />
  </div>
</template>
