<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Compass,
  Copy,
  Download,
  ExternalLink,
  FileUp,
  Layers,
  List,
  Play,
  RefreshCw,
  Search,
} from "lucide-vue-next";
import { getProjectInventory, getProjectInventoryOpenApiUrl } from "@/api/projects";
import type {
  InventoryEndpoint,
  InventoryScenarioMeta,
  ProjectInventoryResponse,
} from "@/api/types";
import MetricCard from "@/components/layout/MetricCard.vue";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import EmptyState from "@/components/layout/EmptyState.vue";
import TryItDrawer, { type TryItEndpointInput } from "@/components/runs/TryItDrawer.vue";
import { useWorkspace } from "@/stores/workspace";
import { useI18n } from "@/i18n";
import { copyToClipboard } from "@/utils/clipboard";

const route = useRoute();
const { t } = useI18n();
const { activeProjectId, workspacePath } = useWorkspace();

const projectId = computed(() =>
  typeof route.params.projectId === "string" && route.params.projectId
    ? route.params.projectId
    : activeProjectId.value
);

const loading = ref(true);
const error = ref<string | null>(null);
const inventory = ref<ProjectInventoryResponse | null>(null);

const searchQuery = ref("");
const methodFilter = ref("ALL");
const sourceFilter = ref("ALL");
const prefixFilter = ref("ALL");
const scenarioFilter = ref("ALL");
const viewMode = ref<"list" | "scenario">("list");

const expandedKeys = ref<Set<string>>(new Set());
const collapsedGroups = ref<Set<string>>(new Set());
const panelModes = ref<Record<string, "example" | "schema">>({});
const copiedKey = ref<string | null>(null);

// Try It Drawer state
const tryItOpen = ref(false);
const tryItEndpoint = ref<TryItEndpointInput | null>(null);

const HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;
const SOURCE_PRIORITY: Record<string, number> = {
  "har-live": 0,
  "openapi-doc": 1,
  "js-static-inferred": 2,
  "js-static": 3,
};

function epKey(ep: InventoryEndpoint): string {
  return `${ep.method.toUpperCase()} ${ep.path}`;
}

const sortedEndpoints = computed<InventoryEndpoint[]>(() => {
  const list = inventory.value?.endpoints ?? [];
  return [...list].sort((a, b) => {
    const pa = SOURCE_PRIORITY[a.discovery_source] ?? 4;
    const pb = SOURCE_PRIORITY[b.discovery_source] ?? 4;
    if (pa !== pb) return pa - pb;
    return a.path.localeCompare(b.path) || a.method.localeCompare(b.method);
  });
});

const sourceCounts = computed(() => {
  const counts: Record<string, number> = {
    "har-live": 0,
    "js-static-inferred": 0,
    "js-static": 0,
    "openapi-doc": 0,
  };
  for (const ep of sortedEndpoints.value) {
    counts[ep.discovery_source] = (counts[ep.discovery_source] ?? 0) + 1;
  }
  return counts;
});

const sourceScopedEndpoints = computed(() => {
  if (sourceFilter.value === "ALL") return sortedEndpoints.value;
  return sortedEndpoints.value.filter((ep) => ep.discovery_source === sourceFilter.value);
});

function getScenarioMeta(slug: string): InventoryScenarioMeta | null {
  return inventory.value?.scenario_meta?.[slug] ?? null;
}

function scenarioPriorityClass(priority: string): string {
  switch (priority) {
    case "P0":
      return "bg-rose-100 text-rose-700";
    case "P1":
      return "bg-amber-100 text-amber-800";
    case "P2":
      return "bg-indigo-100 text-indigo-700";
    default:
      return "bg-slate-200 text-slate-700";
  }
}

const scenarioPills = computed(() => {
  const names = new Set<string>(inventory.value?.scenarios ?? []);
  for (const ep of sortedEndpoints.value) {
    for (const sc of ep.scenarios ?? []) {
      if (sc) names.add(sc);
    }
  }
  if (names.size === 0) return [];
  const counts = new Map<string, number>();
  for (const name of names) counts.set(name, 0);
  for (const ep of sourceScopedEndpoints.value) {
    for (const sc of ep.scenarios ?? []) {
      if (counts.has(sc)) counts.set(sc, (counts.get(sc) ?? 0) + 1);
    }
  }
  return Array.from(counts.entries())
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }))
    .map(([slug, count]) => ({
      slug,
      name: slug,
      count,
      meta: getScenarioMeta(slug),
    }));
});

const filteredEndpoints = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  return sortedEndpoints.value.filter((ep) => {
    if (methodFilter.value !== "ALL" && ep.method.toUpperCase() !== methodFilter.value) {
      return false;
    }
    if (sourceFilter.value !== "ALL" && ep.discovery_source !== sourceFilter.value) {
      return false;
    }
    if (prefixFilter.value !== "ALL" && !ep.path.startsWith(prefixFilter.value)) {
      return false;
    }
    if (scenarioFilter.value !== "ALL" && !(ep.scenarios ?? []).includes(scenarioFilter.value)) {
      return false;
    }
    if (!q) return true;
    return (
      ep.path.toLowerCase().includes(q) ||
      ep.method.toLowerCase().includes(q) ||
      (ep.summary ?? "").toLowerCase().includes(q) ||
      (ep.host ?? "").toLowerCase().includes(q) ||
      (ep.tags ?? []).some((tag) => tag.toLowerCase().includes(q)) ||
      (ep.scenarios ?? []).some((s) => s.toLowerCase().includes(q))
    );
  });
});

interface DisplayGroup {
  id: string;
  slug: string;
  label: string;
  meta: InventoryScenarioMeta | null;
  showHeader: boolean;
  endpoints: InventoryEndpoint[];
  methodCounts: Array<{ method: string; count: number }>;
}

function summarizeMethods(list: InventoryEndpoint[]): Array<{ method: string; count: number }> {
  const order = ["GET", "POST", "PUT", "PATCH", "DELETE"];
  const map = new Map<string, number>();
  for (const ep of list) {
    const m = ep.method.toUpperCase();
    map.set(m, (map.get(m) ?? 0) + 1);
  }
  return Array.from(map.entries())
    .sort(([a], [b]) => {
      const ia = order.indexOf(a);
      const ib = order.indexOf(b);
      if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
      return a.localeCompare(b);
    })
    .map(([method, count]) => ({ method, count }));
}

const displayGroups = computed<DisplayGroup[]>(() => {
  if (viewMode.value === "list") {
    return [
      {
        id: "__list__",
        slug: "",
        label: "",
        meta: null,
        showHeader: false,
        endpoints: filteredEndpoints.value,
        methodCounts: [],
      },
    ];
  }
  const byScenario = new Map<string, InventoryEndpoint[]>();
  const unassigned: InventoryEndpoint[] = [];
  for (const ep of filteredEndpoints.value) {
    const matched =
      scenarioFilter.value !== "ALL"
        ? (ep.scenarios ?? []).filter((s) => s === scenarioFilter.value)
        : (ep.scenarios ?? []);
    if (matched.length === 0) {
      unassigned.push(ep);
    } else {
      for (const sc of matched) {
        const list = byScenario.get(sc) ?? [];
        list.push(ep);
        byScenario.set(sc, list);
      }
    }
  }
  const groups: DisplayGroup[] = Array.from(byScenario.entries())
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }))
    .map(([slug, list]) => {
      const meta = getScenarioMeta(slug);
      return {
        id: `sc:${slug}`,
        slug,
        label: meta?.name || slug,
        meta,
        showHeader: true,
        endpoints: list,
        methodCounts: summarizeMethods(list),
      };
    });
  if (unassigned.length > 0) {
    groups.push({
      id: "__unassigned__",
      slug: "",
      label: t("inv.unassigned"),
      meta: null,
      showHeader: true,
      endpoints: unassigned,
      methodCounts: summarizeMethods(unassigned),
    });
  }
  return groups;
});

function methodBadgeClass(method: string): string {
  switch (method.toUpperCase()) {
    case "GET":
      return "bg-sky-50 text-sky-700 ring-sky-600/20";
    case "POST":
      return "bg-emerald-50 text-emerald-700 ring-emerald-600/20";
    case "PUT":
      return "bg-amber-50 text-amber-700 ring-amber-600/20";
    case "PATCH":
      return "bg-violet-50 text-violet-700 ring-violet-600/20";
    case "DELETE":
      return "bg-rose-50 text-rose-700 ring-rose-600/20";
    default:
      return "bg-slate-100 text-slate-700 ring-slate-600/20";
  }
}

function toggleExpand(key: string): void {
  const next = new Set(expandedKeys.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  expandedKeys.value = next;
}

function toggleGroupCollapse(groupId: string): void {
  const next = new Set(collapsedGroups.value);
  if (next.has(groupId)) next.delete(groupId);
  else next.add(groupId);
  collapsedGroups.value = next;
}

function getPanelMode(
  key: string,
  section: "req" | "resp",
  hasExample: boolean
): "example" | "schema" {
  const explicit = panelModes.value[`${key}::${section}`];
  if (explicit) return explicit;
  return hasExample ? "example" : "schema";
}

function setPanelMode(key: string, section: "req" | "resp", mode: "example" | "schema"): void {
  panelModes.value = { ...panelModes.value, [`${key}::${section}`]: mode };
}

function prettyJson(val: unknown): string {
  try {
    return JSON.stringify(val, null, 2);
  } catch {
    return String(val);
  }
}

async function copyJson(key: string, payload: unknown): Promise<void> {
  if (payload === null || payload === undefined) return;
  const ok = await copyToClipboard(prettyJson(payload));
  if (!ok) return;
  copiedKey.value = key;
  window.setTimeout(() => {
    if (copiedKey.value === key) copiedKey.value = null;
  }, 1800);
}

function openTryIt(ep: InventoryEndpoint): void {
  tryItEndpoint.value = {
    method: ep.method,
    path: ep.path,
    host: ep.host,
    summary: ep.summary,
    parameters: ep.parameters,
    requestExample: ep.request_example,
  };
  tryItOpen.value = true;
}

async function loadInventory(): Promise<void> {
  if (!projectId.value) {
    inventory.value = null;
    loading.value = false;
    return;
  }
  loading.value = true;
  error.value = null;
  expandedKeys.value = new Set();
  collapsedGroups.value = new Set();
  prefixFilter.value = "ALL";
  scenarioFilter.value = "ALL";
  try {
    const res = await getProjectInventory(projectId.value);
    inventory.value = res;
    sourceFilter.value = res.har_live_count > 0 ? "har-live" : "ALL";
  } catch (err) {
    inventory.value = null;
    error.value = err instanceof Error ? err.message : t("inv.loadError");
  } finally {
    loading.value = false;
  }
}

watch(projectId, () => {
  void loadInventory();
});

onMounted(() => {
  void loadInventory();
});
</script>

<template>
  <div class="space-y-5">
    <!-- Loading -->
    <div v-if="loading" class="card py-16 text-center text-sm text-slate-500">
      {{ t("inv.loading") }}
    </div>

    <!-- Error -->
    <div
      v-else-if="error"
      class="flex items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
    >
      <div class="flex items-center gap-2">
        <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
        <span>{{ error }}</span>
      </div>
      <button type="button" class="btn-secondary !py-1 !text-xs" @click="loadInventory">
        {{ t("common.retry") }}
      </button>
    </div>

    <!-- Empty Inventory -->
    <EmptyState
      v-else-if="!inventory || inventory.total_endpoints === 0"
      :title="t('inv.emptyTitle')"
      :description="t('inv.emptyDesc')"
    >
      <template #icon>
        <Layers class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <router-link :to="{ path: workspacePath('runs'), query: { new: '1' } }" class="btn-primary">
          <FileUp class="h-4 w-4" />
          <span>{{ t("inv.ctaNewRun") }}</span>
        </router-link>
        <router-link :to="workspacePath('explore')" class="btn-secondary">
          <Compass class="h-4 w-4 text-indigo-600" />
          <span>{{ t("inv.ctaExplore") }}</span>
        </router-link>
      </template>
    </EmptyState>

    <!-- Main Master API Inventory -->
    <template v-else>
      <!-- Top Action Strip & KPIs -->
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-2">
          <h2 class="text-lg font-bold text-slate-900">{{ inventory.project_name }}</h2>
          <span class="rounded-full bg-indigo-50 px-2.5 py-0.5 text-xs font-semibold text-indigo-700">
            {{ inventory.total_endpoints }} APIs
          </span>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <button type="button" class="btn-secondary !py-1.5 !text-xs" @click="loadInventory">
            <RefreshCw class="h-3.5 w-3.5" />
            <span>{{ t("inv.refresh") }}</span>
          </button>
          <a
            :href="getProjectInventoryOpenApiUrl(projectId)"
            download
            data-testid="inventory-download-openapi"
            class="btn-primary !py-1.5 !text-xs"
          >
            <Download class="h-3.5 w-3.5" />
            <span>{{ t("inv.downloadOpenApi") }}</span>
          </a>
        </div>
      </div>

      <!-- 4 Summary KPIs -->
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          :label="t('inv.kpiTotal')"
          :value="inventory.total_endpoints"
          :subtitle="
            t('inv.kpiTotalSub', {
              runs: inventory.runs_merged_count,
              explores: inventory.explore_sessions_count,
            })
          "
          tone="indigo"
        />
        <MetricCard
          :label="t('inv.kpiLive')"
          :value="inventory.har_live_count"
          :subtitle="t('inv.kpiLiveSub')"
          tone="emerald"
        />
        <MetricCard
          :label="t('inv.kpiStatic')"
          :value="inventory.js_static_count"
          :subtitle="t('inv.kpiStaticSub')"
          tone="amber"
        />
        <MetricCard
          :label="t('inv.kpiCoverage')"
          :value="`${inventory.coverage_pct.toFixed(1)}%`"
          :subtitle="t('inv.kpiCoverageSub')"
          tone="neutral"
        />
      </div>

      <!-- Prefix Breakdown Pills -->
      <div
        v-if="inventory.by_prefix.length > 0"
        class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm"
      >
        <span class="text-xs font-semibold text-slate-600">{{ t("inv.prefixTitle") }}:</span>
        <button
          type="button"
          class="rounded-full px-2.5 py-1 text-xs font-medium transition"
          :class="
            prefixFilter === 'ALL'
              ? 'bg-slate-900 text-white'
              : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
          "
          @click="prefixFilter = 'ALL'"
        >
          {{ t("inv.prefixAll") }}
        </button>
        <button
          v-for="pf in inventory.by_prefix.slice(0, 12)"
          :key="pf.prefix"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-mono text-xs transition"
          :class="
            prefixFilter === pf.prefix
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
          "
          @click="prefixFilter = prefixFilter === pf.prefix ? 'ALL' : pf.prefix"
        >
          <span>{{ pf.prefix }}</span>
          <span class="opacity-75">{{ pf.covered }}/{{ pf.total }}</span>
        </button>
      </div>

      <!-- Search + Method + Source + View Mode Bar -->
      <div class="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
        <div class="relative min-w-[220px] flex-1">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            v-model="searchQuery"
            data-testid="inventory-search"
            type="text"
            :placeholder="t('inv.searchPlaceholder')"
            class="w-full rounded-lg border border-slate-300 py-1.5 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
          />
        </div>

        <!-- HTTP Methods -->
        <div class="flex items-center gap-1 rounded-lg bg-slate-100 p-1">
          <button
            type="button"
            class="rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="methodFilter === 'ALL' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600'"
            @click="methodFilter = 'ALL'"
          >
            {{ t("inv.allMethods") }}
          </button>
          <button
            v-for="m in HTTP_METHODS"
            :key="m"
            type="button"
            class="rounded-md px-2 py-1 font-mono text-xs font-medium transition"
            :class="methodFilter === m ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600'"
            @click="methodFilter = m"
          >
            {{ m }}
          </button>
        </div>

        <!-- Source Filter (har-live prioritized) -->
        <div class="flex flex-wrap items-center gap-1 rounded-lg bg-slate-100 p-1">
          <button
            type="button"
            data-testid="inv-source-har-live"
            class="rounded-md px-2.5 py-1 text-xs font-semibold transition"
            :class="
              sourceFilter === 'har-live'
                ? 'bg-emerald-600 text-white shadow-sm'
                : 'text-emerald-700 hover:bg-emerald-50'
            "
            @click="sourceFilter = 'har-live'"
          >
            {{ t("inv.sourceHarLive") }} ({{ sourceCounts["har-live"] }})
          </button>
          <button
            v-if="sourceCounts['openapi-doc'] > 0"
            type="button"
            class="rounded-md px-2 py-1 text-xs font-medium transition"
            :class="
              sourceFilter === 'openapi-doc' ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-600'
            "
            @click="sourceFilter = 'openapi-doc'"
          >
            {{ t("inv.sourceOpenApi") }} ({{ sourceCounts["openapi-doc"] }})
          </button>
          <button
            type="button"
            class="rounded-md px-2 py-1 text-xs font-medium transition"
            :class="
              sourceFilter === 'js-static-inferred'
                ? 'bg-white text-amber-800 shadow-sm'
                : 'text-slate-600'
            "
            @click="sourceFilter = 'js-static-inferred'"
          >
            {{ t("inv.sourceJsInferred") }} ({{ sourceCounts["js-static-inferred"] }})
          </button>
          <button
            type="button"
            class="rounded-md px-2 py-1 text-xs font-medium transition"
            :class="
              sourceFilter === 'js-static' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'
            "
            @click="sourceFilter = 'js-static'"
          >
            {{ t("inv.sourceJsStatic") }} ({{ sourceCounts["js-static"] }})
          </button>
          <button
            type="button"
            data-testid="inv-source-ALL"
            class="rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="sourceFilter === 'ALL' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'"
            @click="sourceFilter = 'ALL'"
          >
            {{ t("inv.allSources") }} ({{ sortedEndpoints.length }})
          </button>
        </div>

        <!-- View Mode Toggle -->
        <div
          v-if="scenarioPills.length > 0"
          class="ml-auto inline-flex items-center gap-1 rounded-lg bg-slate-100 p-1"
        >
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="viewMode === 'list' ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-600'"
            @click="viewMode = 'list'"
          >
            <List class="h-3.5 w-3.5" />
            <span>{{ t("inv.viewList") }}</span>
          </button>
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="
              viewMode === 'scenario' ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-600'
            "
            @click="viewMode = 'scenario'"
          >
            <Layers class="h-3.5 w-3.5" />
            <span>{{ t("inv.viewScenario") }}</span>
          </button>
        </div>
      </div>

      <!-- Business Scenario Filter Bar -->
      <div
        v-if="scenarioPills.length > 0"
        data-testid="inventory-scenario-bar"
        class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm"
      >
        <span class="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-600">
          <Layers class="h-3.5 w-3.5 text-indigo-600" />
          <span>{{ t("inv.scenariosLabel") }}:</span>
        </span>
        <button
          type="button"
          class="rounded-full px-2.5 py-1 text-xs font-medium transition"
          :class="
            scenarioFilter === 'ALL'
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
          "
          @click="scenarioFilter = 'ALL'"
        >
          {{ t("inv.allScenarios") }} ({{ sourceScopedEndpoints.length }})
        </button>
        <button
          v-for="sc in scenarioPills"
          :key="sc.slug"
          type="button"
          :title="sc.meta?.description || sc.slug"
          class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition"
          :class="
            scenarioFilter === sc.slug
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'bg-indigo-50/70 text-indigo-700 ring-1 ring-inset ring-indigo-200/80 hover:bg-indigo-100'
          "
          @click="scenarioFilter = scenarioFilter === sc.slug ? 'ALL' : sc.slug"
        >
          <span
            v-if="sc.meta?.priority"
            class="rounded px-1 py-0.2 font-mono text-[10px] font-bold"
            :class="
              scenarioFilter === sc.slug
                ? 'bg-white/20 text-white'
                : scenarioPriorityClass(sc.meta.priority)
            "
          >
            {{ sc.meta.priority }}
          </span>
          <span :class="sc.meta?.name ? '' : 'font-mono'">{{ sc.meta?.name || sc.slug }}</span>
          <span class="opacity-75">({{ sc.count }})</span>
        </button>

        <router-link
          v-if="scenarioFilter !== 'ALL' && getScenarioMeta(scenarioFilter)?.id"
          :to="{
            path: workspacePath('scenarios', projectId),
            query: { id: getScenarioMeta(scenarioFilter)!.id },
          }"
          class="ml-auto inline-flex items-center gap-1 text-xs font-semibold text-indigo-600 hover:text-indigo-800 hover:underline"
        >
          <span>{{ t("inv.openScenarioDetail") }}</span>
          <ExternalLink class="h-3 w-3" />
        </router-link>
      </div>

      <!-- Endpoints Table(s) -->
      <div
        v-if="displayGroups.length === 0"
        class="card px-6 py-10 text-center text-sm text-slate-500"
      >
        {{ t("inv.noMatches") }}
      </div>

      <div v-else class="space-y-4">
        <div
          v-for="group in displayGroups"
          :key="group.id"
          class="card overflow-hidden"
        >
          <div
            v-if="group.showHeader"
            class="flex w-full flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50/80 px-4 py-3 transition hover:bg-slate-100/70"
          >
            <button
              type="button"
              class="flex min-w-0 flex-1 items-center gap-2 text-left"
              :title="group.meta?.description || group.slug"
              @click="toggleGroupCollapse(group.id)"
            >
              <ChevronRight v-if="collapsedGroups.has(group.id)" class="h-4 w-4 shrink-0 text-slate-400" />
              <ChevronDown v-else class="h-4 w-4 shrink-0 text-indigo-600" />
              <Layers class="h-4 w-4 shrink-0 text-indigo-600" />
              <span
                v-if="group.meta?.priority"
                class="rounded px-1.5 py-0.5 font-mono text-[10px] font-bold"
                :class="scenarioPriorityClass(group.meta.priority)"
              >
                {{ group.meta.priority }}
              </span>
              <span class="truncate text-sm font-semibold text-slate-900">
                {{ group.label }}
              </span>
              <span
                v-if="group.meta?.effectiveness_level"
                class="rounded bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-emerald-700"
              >
                {{ group.meta.effectiveness_level }}
              </span>
              <span class="rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] font-semibold text-indigo-700">
                {{ t("inv.groupCount", { count: group.endpoints.length }) }}
              </span>
            </button>
            <div class="flex flex-wrap items-center gap-1.5">
              <router-link
                v-if="group.meta?.id"
                :to="{
                  path: workspacePath('scenarios', projectId),
                  query: { id: group.meta.id },
                }"
                class="mr-1 inline-flex items-center gap-1 rounded-md border border-indigo-200 bg-white px-2 py-0.5 text-[11px] font-medium text-indigo-700 hover:bg-indigo-50"
              >
                <span>{{ t("inv.openScenarioDetail") }}</span>
                <ExternalLink class="h-3 w-3" />
              </router-link>
              <span
                v-for="mc in group.methodCounts"
                :key="mc.method"
                class="inline-flex rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ring-1 ring-inset"
                :class="methodBadgeClass(mc.method)"
              >
                {{ mc.method }} {{ mc.count }}
              </span>
            </div>
          </div>

          <div v-if="!group.showHeader || !collapsedGroups.has(group.id)" class="overflow-x-auto">
            <table class="min-w-full divide-y divide-slate-200 text-left text-sm">
              <thead class="table-head">
                <tr>
                  <th class="w-9 px-3 py-3" />
                  <th class="w-24 px-3 py-3">{{ t("inv.colMethod") }}</th>
                  <th class="px-3 py-3">{{ t("inv.colPath") }}</th>
                  <th class="px-3 py-3">{{ t("inv.colSource") }}</th>
                  <th class="px-3 py-3">{{ t("inv.colScenarios") }}</th>
                  <th class="px-3 py-3">{{ t("inv.colRuns") }}</th>
                  <th class="px-3 py-3">{{ t("inv.colStatus") }}</th>
                  <th class="px-4 py-3 text-right">{{ t("inv.colAction") }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100 bg-white">
                <template v-for="ep in group.endpoints" :key="`${group.id}::${epKey(ep)}`">
                  <tr
                    class="cursor-pointer transition hover:bg-slate-50/90"
                    :class="expandedKeys.has(`${group.id}::${epKey(ep)}`) ? 'bg-indigo-50/40' : ''"
                    @click="toggleExpand(`${group.id}::${epKey(ep)}`)"
                  >
                    <td class="px-3 py-3 text-slate-400">
                      <ChevronDown
                        v-if="expandedKeys.has(`${group.id}::${epKey(ep)}`)"
                        class="h-4 w-4 text-indigo-600"
                      />
                      <ChevronRight v-else class="h-4 w-4" />
                    </td>
                    <td class="px-3 py-3">
                      <span
                        class="inline-flex rounded-md px-2 py-0.5 font-mono text-xs font-semibold ring-1 ring-inset"
                        :class="methodBadgeClass(ep.method)"
                      >
                        {{ ep.method.toUpperCase() }}
                      </span>
                    </td>
                    <td class="px-3 py-3">
                      <div class="font-mono text-xs font-medium text-slate-900">{{ ep.path }}</div>
                      <div v-if="ep.host || ep.summary" class="mt-0.5 text-[11px] text-slate-400">
                        <span v-if="ep.host" class="font-mono">{{ ep.host }}</span>
                        <span v-if="ep.host && ep.summary"> · </span>
                        <span v-if="ep.summary">{{ ep.summary }}</span>
                      </div>
                    </td>
                    <td class="px-3 py-3">
                      <StatusBadge :status="ep.discovery_source" size="sm" />
                    </td>
                    <td class="px-3 py-3">
                      <div class="flex flex-wrap gap-1">
                        <span
                          v-for="sc in (ep.scenarios ?? []).slice(0, 3)"
                          :key="sc"
                          :title="getScenarioMeta(sc)?.description || sc"
                          class="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-600"
                        >
                          {{ getScenarioMeta(sc)?.name || sc }}
                        </span>
                        <span
                          v-if="(ep.scenarios ?? []).length > 3"
                          class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500"
                        >
                          +{{ (ep.scenarios ?? []).length - 3 }}
                        </span>
                        <span v-if="(ep.scenarios ?? []).length === 0" class="text-xs text-slate-400">
                          —
                        </span>
                      </div>
                    </td>
                    <td class="px-3 py-3 font-mono text-xs text-slate-600">
                      {{ (ep.seen_in_runs ?? []).length || "—" }}
                    </td>
                    <td class="px-3 py-3">
                      <div class="flex flex-wrap gap-1">
                        <span
                          v-for="code in (ep.status_codes ?? []).slice(0, 3)"
                          :key="code"
                          class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-slate-700"
                        >
                          {{ code }}
                        </span>
                        <span v-if="(ep.status_codes ?? []).length === 0" class="text-xs text-slate-400">
                          —
                        </span>
                      </div>
                    </td>
                    <td class="whitespace-nowrap px-4 py-3 text-right" @click.stop>
                      <button
                        type="button"
                        data-testid="inventory-try-it-btn"
                        class="inline-flex items-center gap-1 rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-xs font-semibold text-indigo-700 transition hover:bg-indigo-100"
                        @click="openTryIt(ep)"
                      >
                        <Play class="h-3 w-3" />
                        <span>{{ t("inv.tryItBtn") }}</span>
                      </button>
                    </td>
                  </tr>

                  <!-- Expanded Detail Row -->
                  <tr v-if="expandedKeys.has(`${group.id}::${epKey(ep)}`)" class="bg-slate-50/70">
                    <td colspan="8" class="px-6 py-4">
                      <div class="space-y-4">
                        <!-- Seen in Runs + Status Codes -->
                        <div class="flex flex-wrap items-center justify-between gap-3 text-xs">
                          <div class="flex flex-wrap items-center gap-2">
                            <template v-if="(ep.seen_in_runs ?? []).length > 0">
                              <span class="font-semibold text-slate-500">
                                {{ t("inv.seenInRunsLabel") }}
                              </span>
                              <router-link
                                v-for="rRef in ep.seen_in_runs"
                                :key="rRef.run_id"
                                :to="`/p/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(rRef.run_id)}`"
                                class="rounded-md border border-indigo-200 bg-white px-2 py-0.5 text-[11px] font-medium text-indigo-700 hover:bg-indigo-50"
                              >
                                {{ rRef.run_name || rRef.run_id.slice(0, 8) }}
                              </router-link>
                            </template>
                          </div>

                          <button
                            type="button"
                            class="btn-primary !py-1 !text-xs"
                            @click="openTryIt(ep)"
                          >
                            <Play class="h-3.5 w-3.5" />
                            <span>{{ t("inv.tryItBtn") }}</span>
                          </button>
                        </div>

                        <!-- Request & Response Example / Schema -->
                        <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
                          <!-- Request -->
                          <div class="rounded-lg border border-slate-200 bg-white p-3.5">
                            <div class="mb-2 flex items-center justify-between gap-2">
                              <h4 class="text-xs font-semibold uppercase tracking-wide text-slate-600">
                                {{ t("inv.reqTitle") }}
                              </h4>
                              <div
                                v-if="ep.request_example !== undefined && ep.request_example !== null || ep.request_schema"
                                class="flex items-center gap-1.5"
                              >
                                <div class="inline-flex rounded-md bg-slate-100 p-0.5 text-[11px]">
                                  <button
                                    type="button"
                                    class="rounded px-2 py-0.5 font-medium"
                                    :class="
                                      getPanelMode(epKey(ep), 'req', ep.request_example !== undefined && ep.request_example !== null) === 'example'
                                        ? 'bg-white text-indigo-700 shadow-sm'
                                        : 'text-slate-500'
                                    "
                                    @click="setPanelMode(epKey(ep), 'req', 'example')"
                                  >
                                    {{ t("inv.tabExample") }}
                                  </button>
                                  <button
                                    type="button"
                                    class="rounded px-2 py-0.5 font-medium"
                                    :class="
                                      getPanelMode(epKey(ep), 'req', ep.request_example !== undefined && ep.request_example !== null) === 'schema'
                                        ? 'bg-white text-indigo-700 shadow-sm'
                                        : 'text-slate-500'
                                    "
                                    @click="setPanelMode(epKey(ep), 'req', 'schema')"
                                  >
                                    {{ t("inv.tabSchema") }}
                                  </button>
                                </div>
                                <button
                                  type="button"
                                  class="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
                                  @click="
                                    copyJson(
                                      `${epKey(ep)}::req`,
                                      getPanelMode(epKey(ep), 'req', ep.request_example !== undefined && ep.request_example !== null) === 'example'
                                        ? ep.request_example
                                        : ep.request_schema
                                    )
                                  "
                                >
                                  <Check v-if="copiedKey === `${epKey(ep)}::req`" class="h-3 w-3 text-emerald-600" />
                                  <Copy v-else class="h-3 w-3" />
                                  <span>
                                    {{ copiedKey === `${epKey(ep)}::req` ? t("inv.copied") : t("inv.copyJson") }}
                                  </span>
                                </button>
                              </div>
                            </div>
                            <div
                              v-if="(ep.request_example === undefined || ep.request_example === null) && !ep.request_schema"
                              class="text-xs text-slate-400"
                            >
                              {{ t("inv.noReqBody") }}
                            </div>
                            <pre
                              v-else
                              class="max-h-60 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-sky-200"
                            >{{
                              prettyJson(
                                getPanelMode(epKey(ep), "req", ep.request_example !== undefined && ep.request_example !== null) === "example"
                                  ? (ep.request_example ?? ep.request_schema)
                                  : (ep.request_schema ?? ep.request_example)
                              )
                            }}</pre>
                          </div>

                          <!-- Response -->
                          <div class="rounded-lg border border-slate-200 bg-white p-3.5">
                            <div class="mb-2 flex items-center justify-between gap-2">
                              <h4 class="text-xs font-semibold uppercase tracking-wide text-slate-600">
                                {{ t("inv.respTitle") }}
                              </h4>
                              <div
                                v-if="(ep.response_example !== undefined && ep.response_example !== null) || ep.response_schema"
                                class="flex items-center gap-1.5"
                              >
                                <div class="inline-flex rounded-md bg-slate-100 p-0.5 text-[11px]">
                                  <button
                                    type="button"
                                    class="rounded px-2 py-0.5 font-medium"
                                    :class="
                                      getPanelMode(epKey(ep), 'resp', ep.response_example !== undefined && ep.response_example !== null) === 'example'
                                        ? 'bg-white text-emerald-700 shadow-sm'
                                        : 'text-slate-500'
                                    "
                                    @click="setPanelMode(epKey(ep), 'resp', 'example')"
                                  >
                                    {{ t("inv.tabExample") }}
                                  </button>
                                  <button
                                    type="button"
                                    class="rounded px-2 py-0.5 font-medium"
                                    :class="
                                      getPanelMode(epKey(ep), 'resp', ep.response_example !== undefined && ep.response_example !== null) === 'schema'
                                        ? 'bg-white text-indigo-700 shadow-sm'
                                        : 'text-slate-500'
                                    "
                                    @click="setPanelMode(epKey(ep), 'resp', 'schema')"
                                  >
                                    {{ t("inv.tabSchema") }}
                                  </button>
                                </div>
                                <button
                                  type="button"
                                  class="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
                                  @click="
                                    copyJson(
                                      `${epKey(ep)}::resp`,
                                      getPanelMode(epKey(ep), 'resp', ep.response_example !== undefined && ep.response_example !== null) === 'example'
                                        ? ep.response_example
                                        : ep.response_schema
                                    )
                                  "
                                >
                                  <Check v-if="copiedKey === `${epKey(ep)}::resp`" class="h-3 w-3 text-emerald-600" />
                                  <Copy v-else class="h-3 w-3" />
                                  <span>
                                    {{ copiedKey === `${epKey(ep)}::resp` ? t("inv.copied") : t("inv.copyJson") }}
                                  </span>
                                </button>
                              </div>
                            </div>
                            <div
                              v-if="(ep.response_example === undefined || ep.response_example === null) && !ep.response_schema"
                              class="text-xs text-slate-400"
                            >
                              {{ t("inv.noRespBody") }}
                            </div>
                            <pre
                              v-else
                              class="max-h-60 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-emerald-200"
                            >{{
                              prettyJson(
                                getPanelMode(epKey(ep), "resp", ep.response_example !== undefined && ep.response_example !== null) === "example"
                                  ? (ep.response_example ?? ep.response_schema)
                                  : (ep.response_schema ?? ep.response_example)
                              )
                            }}</pre>
                          </div>
                        </div>
                      </div>
                    </td>
                  </tr>
                </template>

                <tr v-if="group.endpoints.length === 0">
                  <td colspan="8" class="px-6 py-10 text-center text-sm text-slate-500">
                    {{ t("inv.noMatches") }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>

    <!-- Reusable Inline Try It Drawer -->
    <TryItDrawer
      :open="tryItOpen"
      :project-id="projectId"
      :endpoint="tryItEndpoint"
      @close="tryItOpen = false"
    />
  </div>
</template>
