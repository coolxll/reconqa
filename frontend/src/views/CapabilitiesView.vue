<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import {
  AlertTriangle,
  Boxes,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  GitCommitHorizontal,
  Loader2,
  Play,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from "lucide-vue-next";
import { getProjectCapabilities, promoteCapabilityToScenario } from "@/api/projects";
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

const route = useRoute();
const { t } = useI18n();
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

// Filters (Live Only ON by default!)
const searchQuery = ref("");
const liveOnly = ref(true);
const hasWrites = ref(false);
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

let loadSeq = 0;
let searchDebounceTimer: number | null = null;

const metrics = computed(() => data.value?.metrics ?? null);
const modules = computed(() => data.value?.modules ?? []);
const capabilities = computed(() => data.value?.capabilities ?? []);

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
    searchQuery.value.trim().length > 0 ||
    liveOnly.value ||
    hasWrites.value ||
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
      live_only: liveOnly.value,
      has_writes: hasWrites.value,
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
  searchQuery.value = "";
  liveOnly.value = false;
  hasWrites.value = false;
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
    summary: `${ep.role} · ${ep.action}`,
  };
  tryItOpen.value = true;
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
    });
    if (projectId.value !== pid) return;
    promotedBanner.value = {
      capabilityName: cap.display_name || cap.key,
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

function orderedSlots(cap: BusinessCapability): CapabilitySlotStatus[] {
  const bySlot = new Map<CapabilitySlotName, CapabilitySlotStatus>();
  for (const s of cap.case_matrix ?? []) {
    bySlot.set(s.slot, s);
  }
  return SLOT_ORDER.map(
    (slot) => bySlot.get(slot) ?? { slot, status: "missing", count: 0 }
  );
}

function slotPillClass(status: CapabilitySlotStatus["status"]): string {
  switch (status) {
    case "covered":
      return "border-emerald-200 bg-emerald-50 text-emerald-800";
    case "failed":
      return "border-rose-200 bg-rose-50 text-rose-700";
    case "pending":
      return "border-amber-200 bg-amber-50 text-amber-800";
    default:
      return "border-slate-200 bg-slate-50 text-slate-500";
  }
}

function slotDotClass(status: CapabilitySlotStatus["status"]): string {
  switch (status) {
    case "covered":
      return "bg-emerald-500";
    case "failed":
      return "bg-rose-500";
    case "pending":
      return "bg-amber-400";
    default:
      return "bg-slate-300";
  }
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

function formatStateEntries(observed: Record<string, string[]>): Array<[string, string]> {
  return Object.entries(observed || {})
    .filter(([, vals]) => Array.isArray(vals) && vals.length > 0)
    .slice(0, 4)
    .map(([field, vals]) => [field, vals.slice(0, 8).join(", ") + (vals.length > 8 ? "…" : "")]);
}

function formatPct(val: number | undefined): string {
  return `${Math.round(Number(val ?? 0))}%`;
}

watch(projectId, () => {
  expandedKeys.value = {};
  promotedBanner.value = null;
  promoteError.value = null;
  selectedModule.value = "ALL";
  selectedRole.value = "ALL";
  searchQuery.value = "";
  liveOnly.value = true;
  hasWrites.value = false;
  void fetchCapabilities();
});

watch([liveOnly, hasWrites, selectedRole, selectedModule], () => {
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
  <div class="space-y-5 pb-8">
    <!-- Top 5-Dimensional QA Quality KPI Strip -->
    <section
      v-if="metrics"
      data-testid="cap-kpi-strip"
      class="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-5"
    >
      <!-- 1. Capability Compression & Coverage -->
      <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-slate-500">
            {{ t("cap.kpi.compressionTitle") }}
          </span>
          <Boxes class="h-4 w-4 text-indigo-600" />
        </div>
        <div class="mt-2 flex items-baseline gap-2">
          <span class="font-mono text-2xl font-bold text-slate-900">
            {{ metrics.live_capabilities }}
            <span class="text-sm font-medium text-slate-400">/ {{ metrics.total_capabilities }}</span>
          </span>
          <span class="rounded-full bg-indigo-50 px-2 py-0.5 font-mono text-[11px] font-semibold text-indigo-700">
            {{ formatPct(metrics.capability_coverage_pct) }}
          </span>
        </div>
        <p class="mt-1.5 text-[11px] text-slate-500">
          {{
            t("cap.kpi.compressionSub", {
              endpoints: metrics.total_endpoints,
              pct: Math.round(metrics.capability_coverage_pct),
            })
          }}
        </p>
      </div>

      <!-- 2. State Machine Coverage -->
      <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-slate-500">
            {{ t("cap.kpi.stateTitle") }}
          </span>
          <GitCommitHorizontal class="h-4 w-4 text-violet-600" />
        </div>
        <div class="mt-2 flex items-baseline gap-2">
          <span class="font-mono text-2xl font-bold text-slate-900">
            {{ metrics.state_transition_covered }}
            <span class="text-sm font-medium text-slate-400">/ {{ metrics.stateful_capabilities }}</span>
          </span>
          <span class="rounded-full bg-violet-50 px-2 py-0.5 font-mono text-[11px] font-semibold text-violet-700">
            {{ formatPct(metrics.state_coverage_pct) }}
          </span>
        </div>
        <p class="mt-1.5 text-[11px] text-slate-500">
          {{
            t("cap.kpi.stateSub", {
              pct: Math.round(metrics.state_coverage_pct),
              total: metrics.stateful_capabilities,
            })
          }}
        </p>
      </div>

      <!-- 3. Role AuthZ Matrix -->
      <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-slate-500">
            {{ t("cap.kpi.roleTitle") }}
          </span>
          <Users class="h-4 w-4 text-sky-600" />
        </div>
        <div class="mt-2 flex items-baseline gap-2">
          <span class="font-mono text-2xl font-bold text-slate-900">
            {{ formatPct(metrics.role_matrix_coverage_pct) }}
          </span>
        </div>
        <p class="mt-1.5 text-[11px] text-slate-500">
          {{
            t("cap.kpi.roleSub", {
              covered: metrics.role_matrix_cells_covered,
              total: metrics.role_matrix_cells_total,
            })
          }}
        </p>
      </div>

      <!-- 4. Negative Path Coverage -->
      <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-slate-500">
            {{ t("cap.kpi.negativeTitle") }}
          </span>
          <AlertTriangle class="h-4 w-4 text-amber-600" />
        </div>
        <div class="mt-2 flex items-baseline gap-2">
          <span class="font-mono text-2xl font-bold text-slate-900">
            {{ formatPct(metrics.negative_coverage_pct) }}
          </span>
        </div>
        <p class="mt-1.5 text-[11px] text-slate-500">
          {{
            t("cap.kpi.negativeSub", {
              tested: metrics.negative_slots_tested,
              total: metrics.negative_slots_total,
            })
          }}
        </p>
      </div>

      <!-- 5. Invariant Assertion Pass Rate -->
      <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium text-slate-500">
            {{ t("cap.kpi.invariantTitle") }}
          </span>
          <ShieldCheck class="h-4 w-4 text-emerald-600" />
        </div>
        <div class="mt-2 flex items-baseline gap-2">
          <span class="font-mono text-2xl font-bold text-slate-900">
            {{ formatPct(metrics.invariant_pass_rate_pct) }}
          </span>
        </div>
        <p class="mt-1.5 text-[11px] text-slate-500">
          {{
            t("cap.kpi.invariantSub", {
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

    <!-- Filter & Module Bar -->
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

        <!-- Toggle: Live Only (ON by default) -->
        <button
          type="button"
          data-testid="cap-filter-live-only"
          :aria-pressed="liveOnly"
          class="inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition"
          :class="
            liveOnly
              ? 'border-indigo-600 bg-indigo-600 text-white shadow-xs'
              : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50'
          "
          @click="liveOnly = !liveOnly"
        >
          <span
            class="h-1.5 w-1.5 rounded-full"
            :class="liveOnly ? 'bg-emerald-300' : 'bg-slate-400'"
          />
          <span>{{ t("cap.filter.liveOnly") }}</span>
        </button>

        <!-- Toggle: Has Writes -->
        <button
          type="button"
          data-testid="cap-filter-has-writes"
          :aria-pressed="hasWrites"
          class="inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition"
          :class="
            hasWrites
              ? 'border-amber-500 bg-amber-500 text-white shadow-xs'
              : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50'
          "
          @click="hasWrites = !hasWrites"
        >
          <span>{{ t("cap.filter.hasWrites") }}</span>
        </button>

        <!-- Role Dropdown -->
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

      <!-- Clickable Module Pills -->
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

    <!-- Business Capability Matrix Cards -->
    <section v-else class="space-y-3">
      <article
        v-for="cap in capabilities"
        :key="cap.key"
        data-testid="capability-card"
        class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm transition hover:border-indigo-200"
      >
        <!-- Card Summary Header (Click to Expand Endpoints Accordion) -->
        <div
          class="cursor-pointer px-5 py-4 transition hover:bg-slate-50/60"
          @click="toggleExpand(cap.key)"
        >
          <div class="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <!-- Left: Priority, Display Name, Ratio Bar, Metadata Pills -->
            <div class="min-w-0 flex-1 space-y-2.5">
              <div class="flex flex-wrap items-center gap-2">
                <ChevronDown
                  v-if="expandedKeys[cap.key]"
                  class="h-4 w-4 shrink-0 text-slate-500"
                />
                <ChevronRight v-else class="h-4 w-4 shrink-0 text-slate-400" />

                <span
                  class="rounded border px-1.5 py-0.5 font-mono text-[11px] font-bold"
                  :class="priorityBadgeClass(cap.recommended_priority)"
                >
                  {{ cap.recommended_priority }}
                </span>

                <h3 class="font-mono text-sm font-semibold text-slate-900">
                  {{ cap.display_name || cap.key }}
                </h3>

                <!-- Live / Total & Writes Compact Bar -->
                <div class="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11px]">
                  <span class="font-mono font-semibold text-slate-700">
                    {{ t("cap.liveRatio", { live: cap.live_endpoints, total: cap.total_endpoints }) }}
                  </span>
                  <div class="h-1.5 w-12 overflow-hidden rounded-full bg-slate-200">
                    <div
                      class="h-full rounded-full bg-emerald-500"
                      :style="{ width: `${Math.min(100, Math.round(cap.live_pct))}%` }"
                    />
                  </div>
                  <span
                    v-if="cap.write_endpoints > 0"
                    class="rounded bg-amber-100 px-1.5 py-0.2 font-mono text-[10px] font-semibold text-amber-800"
                  >
                    {{ t("cap.writesBadge", { count: cap.write_endpoints }) }}
                  </span>
                </div>

                <span
                  v-if="cap.has_state_machine"
                  class="inline-flex items-center gap-1 rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-[11px] font-semibold text-violet-700"
                >
                  <GitCommitHorizontal class="h-3 w-3" />
                  <span>{{ t("cap.stateMachineBadge") }}</span>
                </span>
              </div>

              <!-- Roles, Actions, and Observed State Machine Enums -->
              <div class="flex flex-wrap items-center gap-x-4 gap-y-1.5 pl-6 text-[11px]">
                <!-- Roles -->
                <div v-if="cap.roles.length > 0" class="flex flex-wrap items-center gap-1">
                  <span class="font-medium text-slate-400">{{ t("cap.rolesLabel") }}:</span>
                  <span
                    v-for="role in cap.roles"
                    :key="`${cap.key}-role-${role}`"
                    class="rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold"
                    :class="roleBadgeClass(role)"
                  >
                    {{ role }}
                  </span>
                </div>

                <!-- Actions -->
                <div v-if="cap.actions.length > 0" class="flex flex-wrap items-center gap-1">
                  <span class="font-medium text-slate-400">{{ t("cap.actionsLabel") }}:</span>
                  <span
                    v-for="action in cap.actions"
                    :key="`${cap.key}-act-${action}`"
                    class="rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[10px] font-medium text-slate-700"
                  >
                    {{ action }}
                  </span>
                </div>

                <!-- Observed Status Enums -->
                <div
                  v-if="formatStateEntries(cap.observed_statuses).length > 0"
                  class="flex flex-wrap items-center gap-1"
                >
                  <span class="font-medium text-slate-400">{{ t("cap.statesLabel") }}:</span>
                  <span
                    v-for="[field, vals] in formatStateEntries(cap.observed_statuses)"
                    :key="`${cap.key}-st-${field}`"
                    class="rounded border border-violet-200 bg-violet-50/70 px-1.5 py-0.5 font-mono text-[10px] font-medium text-violet-800"
                  >
                    {{ field }}: {{ vals }}
                  </span>
                </div>
              </div>

              <!-- 5-Slot Case Matrix Strip -->
              <div class="flex flex-wrap items-center gap-1.5 pl-6">
                <span class="mr-1 text-[11px] font-medium text-slate-400">
                  {{ t("cap.caseMatrixLabel") }}:
                </span>
                <span
                  v-for="slotItem in orderedSlots(cap)"
                  :key="`${cap.key}-slot-${slotItem.slot}`"
                  :title="`${t(`cap.slot.${slotItem.slot}`)} · ${t(`cap.slotStatus.${slotItem.status}`)} (${slotItem.count})`"
                  class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium"
                  :class="slotPillClass(slotItem.status)"
                >
                  <span class="h-1.5 w-1.5 rounded-full" :class="slotDotClass(slotItem.status)" />
                  <span>{{ t(`cap.slot.${slotItem.slot}`) }}</span>
                  <span v-if="slotItem.count > 0" class="font-mono text-[10px] opacity-80">
                    {{ slotItem.count }}
                  </span>
                </span>
                <span class="ml-1 font-mono text-[11px] font-semibold text-slate-600">
                  {{ t("cap.caseCoveragePct", { pct: Math.round(cap.case_coverage_pct) }) }}
                </span>
              </div>
            </div>

            <!-- Right: Linked Scenarios & Promote to 5-Case Scenario CTA -->
            <div
              class="flex shrink-0 flex-wrap items-center gap-2 pl-6 lg:flex-col lg:items-end lg:pl-0"
              @click.stop
            >
              <button
                type="button"
                data-testid="cap-promote-btn"
                :disabled="promotingKey === cap.key"
                class="inline-flex items-center gap-1.5 rounded-lg border border-indigo-600 bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white shadow-xs transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
                @click="handlePromote(cap)"
              >
                <Loader2 v-if="promotingKey === cap.key" class="h-3.5 w-3.5 animate-spin" />
                <Sparkles v-else class="h-3.5 w-3.5" />
                <span>
                  {{
                    promotingKey === cap.key
                      ? t("cap.promoting")
                      : cap.linked_scenarios.length > 0
                        ? t("cap.promoteUpdateBtn")
                        : t("cap.promoteBtn")
                  }}
                </span>
              </button>

              <div
                v-if="cap.linked_scenarios.length > 0"
                class="flex flex-wrap items-center justify-end gap-1"
              >
                <RouterLink
                  v-for="sc in cap.linked_scenarios.slice(0, 2)"
                  :key="sc.id"
                  :to="{
                    path: `/p/${encodeURIComponent(projectId)}/scenarios`,
                    query: { id: sc.id },
                  }"
                  data-testid="cap-linked-scenario-link"
                  class="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-800 transition hover:bg-emerald-100"
                >
                  <span class="max-w-[140px] truncate">{{ sc.name }}</span>
                  <ExternalLink class="h-3 w-3 shrink-0" />
                </RouterLink>
              </div>
            </div>
          </div>
        </div>

        <!-- Expanded Endpoints Detail Accordion -->
        <div
          v-if="expandedKeys[cap.key]"
          data-testid="cap-endpoints-drawer"
          class="border-t border-slate-200 bg-slate-50/60"
        >
          <div class="flex items-center justify-between border-b border-slate-200/80 px-5 py-2.5">
            <span class="text-xs font-semibold text-slate-700">
              {{ t("cap.endpointsHeader", { count: cap.endpoints.length }) }}
            </span>
          </div>

          <div v-if="cap.endpoints.length === 0" class="px-5 py-6 text-center text-xs text-slate-500">
            {{ t("cap.noEndpoints") }}
          </div>

          <div v-else class="overflow-x-auto">
            <table class="min-w-full divide-y divide-slate-200 text-left text-xs">
              <thead class="bg-white/80 font-semibold text-slate-500 uppercase">
                <tr>
                  <th class="px-5 py-2">{{ t("cap.col.method") }}</th>
                  <th class="px-4 py-2">{{ t("cap.col.path") }}</th>
                  <th class="px-3 py-2">{{ t("cap.col.source") }}</th>
                  <th class="px-3 py-2">{{ t("cap.col.role") }}</th>
                  <th class="px-3 py-2">{{ t("cap.col.action") }}</th>
                  <th class="px-5 py-2 text-right">{{ t("cap.col.replay") }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100 bg-white">
                <tr
                  v-for="(ep, idx) in cap.endpoints"
                  :key="`${cap.key}-ep-${idx}-${ep.method}-${ep.path}`"
                  class="hover:bg-slate-50/80"
                >
                  <td class="px-5 py-2">
                    <span
                      class="inline-flex rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold"
                      :class="methodBadgeClass(ep.method)"
                    >
                      {{ ep.method }}
                    </span>
                  </td>
                  <td class="px-4 py-2 font-mono text-slate-800">
                    <div class="break-all">{{ ep.path }}</div>
                    <div v-if="ep.host" class="text-[10px] text-slate-400">{{ ep.host }}</div>
                  </td>
                  <td class="px-3 py-2">
                    <StatusBadge :status="ep.discovery_source" size="sm" />
                  </td>
                  <td class="px-3 py-2">
                    <span
                      class="rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold"
                      :class="roleBadgeClass(ep.role)"
                    >
                      {{ ep.role }}
                    </span>
                  </td>
                  <td class="px-3 py-2 font-mono text-[11px] text-slate-600">
                    {{ ep.action }}
                  </td>
                  <td class="px-5 py-2 text-right">
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
      </article>
    </section>

    <!-- Inline Try It Replay Drawer -->
    <TryItDrawer
      :open="tryItOpen"
      :project-id="projectId"
      :endpoint="tryItEndpoint"
      @close="tryItOpen = false"
    />
  </div>
</template>
