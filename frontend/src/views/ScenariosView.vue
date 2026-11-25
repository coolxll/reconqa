<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Code2,
  Database,
  ExternalLink,
  ListChecks,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Save,
  Search,
  ShieldAlert,
  Sparkles,
  Tag,
  Trash2,
  User,
  X,
} from "lucide-vue-next";
import {
  createScenario,
  deleteScenario,
  getScenarioDetail,
  listProjectScenarios,
  syncProjectScenarios,
  updateScenario,
} from "@/api/scenarios";
import type {
  InventoryEndpoint,
  ScenarioAssertion,
  ScenarioAssertionOperator,
  ScenarioAssertionStatus,
  ScenarioDetail,
  ScenarioEffectivenessLevel,
  ScenarioItem,
  ScenarioListResponse,
  ScenarioNegativeCase,
  ScenarioNegativeCategory,
  ScenarioNegativeStatus,
  ScenarioPriority,
  ScenarioStatus,
} from "@/api/types";
import { useWorkspace } from "@/stores/workspace";
import { useI18n } from "@/i18n";
import "@/i18n/locales/scenarios";
import {
  ASSERTION_OPERATOR_GROUPS,
  filterScenarioAssertionsForEndpoint,
  getOperatorMeta,
  isSqlOperator,
} from "@/utils/assertionOperators";
import EmptyState from "@/components/layout/EmptyState.vue";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import TryItDrawer, { type TryItEndpointInput } from "@/components/runs/TryItDrawer.vue";

const PRIORITIES: ScenarioPriority[] = ["P0", "P1", "P2", "P3"];
const EFFECTIVENESS_LEVELS: ScenarioEffectivenessLevel[] = ["L0", "L1", "L2", "L3", "L4"];
const STATUSES: ScenarioStatus[] = ["active", "verified", "draft", "archived"];
const NEGATIVE_CATEGORIES: ScenarioNegativeCategory[] = [
  "auth",
  "validation",
  "boundary",
  "idempotency",
  "state_machine",
  "security",
];

const route = useRoute();
const router = useRouter();
const { t, locale } = useI18n();
const { activeProjectId, ensureLoaded } = useWorkspace();

const projectId = computed(
  () =>
    (typeof route.params.projectId === "string" && route.params.projectId) ||
    activeProjectId.value ||
    ""
);

// List state
const loading = ref(true);
const syncing = ref(false);
const errorMsg = ref<string | null>(null);
const successMsg = ref<string | null>(null);
const listData = ref<ScenarioListResponse | null>(null);

// Filters
const searchQuery = ref("");
const priorityFilter = ref<"ALL" | ScenarioPriority>("ALL");
const levelFilter = ref<"ALL" | ScenarioEffectivenessLevel>("ALL");
const statusFilter = ref<"ALL" | ScenarioStatus>("ALL");
const domainFilter = ref<string>("ALL");

// Detail drawer state
const selectedScenarioId = ref<string | null>(null);
const loadingDetail = ref(false);
const detail = ref<ScenarioDetail | null>(null);
const drawerTab = ref<"spec" | "assertions" | "negatives" | "endpoints">("spec");
const savingDetail = ref(false);
const confirmingDelete = ref(false);
const deletingDetail = ref(false);
const expandedEndpointKeys = ref<Set<string>>(new Set());

// Editable spec fields
const editName = ref("");
const editPriority = ref<ScenarioPriority>("P2");
const editStatus = ref<ScenarioStatus>("active");
const editDomain = ref("");
const editOwner = ref("");
const editTags = ref("");
const editDescription = ref("");
const editPreconditions = ref("");
const editAssertions = ref<ScenarioAssertion[]>([]);
const editNegatives = ref<ScenarioNegativeCase[]>([]);

// New assertion form
const newAssertTitle = ref("");
const newAssertEndpoint = ref("");
const newAssertJsonpath = ref("");
const newAssertOperator = ref<ScenarioAssertionOperator>("not_empty");
const newAssertExpected = ref("");
const newAssertDbDsnRef = ref("");
const newAssertNote = ref("");

const activeOperatorMeta = computed(() => getOperatorMeta(newAssertOperator.value));

// New negative case form
const newNegTitle = ref("");
const newNegCategory = ref<ScenarioNegativeCategory>("validation");
const newNegExpected = ref("");
const newNegNote = ref("");

// Create Scenario Modal state
const createModalOpen = ref(false);
const creating = ref(false);
const createName = ref("");
const createSlug = ref("");
const createPriority = ref<ScenarioPriority>("P1");
const createDomain = ref("");
const createOwner = ref("");
const createDescription = ref("");
const createPreconditions = ref("");

// Try It Drawer state
const tryItOpen = ref(false);
const tryItEndpoint = ref<TryItEndpointInput | null>(null);
const tryItAssertions = ref<ScenarioAssertion[]>([]);

const scenarios = computed<ScenarioItem[]>(() => listData.value?.scenarios ?? []);

const availableDomains = computed<string[]>(() => {
  const set = new Set<string>();
  for (const s of scenarios.value) {
    if (s.business_domain?.trim()) set.add(s.business_domain.trim());
  }
  return Array.from(set).sort((a, b) => a.localeCompare(b));
});

const filteredScenarios = computed<ScenarioItem[]>(() => {
  const q = searchQuery.value.trim().toLowerCase();
  return scenarios.value.filter((item) => {
    if (priorityFilter.value !== "ALL" && item.priority !== priorityFilter.value) return false;
    if (levelFilter.value !== "ALL" && item.effectiveness_level !== levelFilter.value) return false;
    if (statusFilter.value !== "ALL" && item.status !== statusFilter.value) return false;
    if (domainFilter.value !== "ALL" && item.business_domain !== domainFilter.value) return false;
    if (!q) return true;
    const hay = [
      item.name,
      item.slug,
      item.business_domain,
      item.owner,
      item.description,
      ...(item.tags ?? []),
    ]
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
});

function showSuccess(msg: string): void {
  errorMsg.value = null;
  successMsg.value = msg;
  window.setTimeout(() => {
    if (successMsg.value === msg) successMsg.value = null;
  }, 4000);
}

function priorityBadgeClass(priority: string): string {
  switch (priority) {
    case "P0":
      return "bg-rose-50 text-rose-700 ring-rose-600/25";
    case "P1":
      return "bg-amber-50 text-amber-800 ring-amber-600/25";
    case "P2":
      return "bg-indigo-50 text-indigo-700 ring-indigo-600/20";
    default:
      return "bg-slate-100 text-slate-600 ring-slate-500/20";
  }
}

function levelBadgeClass(level: string): string {
  switch (level) {
    case "L4":
      return "bg-emerald-50 text-emerald-700 ring-emerald-600/25";
    case "L3":
      return "bg-teal-50 text-teal-700 ring-teal-600/25";
    case "L2":
      return "bg-sky-50 text-sky-700 ring-sky-600/25";
    case "L1":
      return "bg-slate-100 text-slate-700 ring-slate-500/20";
    default:
      return "bg-slate-50 text-slate-500 ring-slate-400/20";
  }
}

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

function assertionStatusClass(status: ScenarioAssertionStatus): string {
  if (status === "passed") return "bg-emerald-50 text-emerald-700 ring-emerald-600/20";
  if (status === "failed") return "bg-rose-50 text-rose-700 ring-rose-600/20";
  return "bg-slate-100 text-slate-600 ring-slate-500/20";
}

function negativeStatusClass(status: ScenarioNegativeStatus): string {
  if (status === "covered") return "bg-emerald-50 text-emerald-700 ring-emerald-600/20";
  if (status === "failed") return "bg-rose-50 text-rose-700 ring-rose-600/20";
  return "bg-slate-100 text-slate-600 ring-slate-500/20";
}

function nextAssertionStatus(current: ScenarioAssertionStatus): ScenarioAssertionStatus {
  if (current === "pending") return "passed";
  if (current === "passed") return "failed";
  return "pending";
}

function nextNegativeStatus(current: ScenarioNegativeStatus): ScenarioNegativeStatus {
  if (current === "pending") return "covered";
  if (current === "covered") return "failed";
  return "pending";
}

function genLocalId(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

function hydrateEditForm(d: ScenarioDetail): void {
  editName.value = d.name;
  editPriority.value = d.priority;
  editStatus.value = d.status;
  editDomain.value = d.business_domain || "";
  editOwner.value = d.owner || "";
  editTags.value = (d.tags ?? []).join(", ");
  editDescription.value = d.description || "";
  editPreconditions.value = d.preconditions || "";
  editAssertions.value = (d.assertions ?? []).map((a) => ({ ...a }));
  editNegatives.value = (d.negative_cases ?? []).map((n) => ({ ...n }));
}

async function loadScenarios(): Promise<void> {
  if (!projectId.value) {
    listData.value = null;
    loading.value = false;
    return;
  }
  loading.value = true;
  errorMsg.value = null;
  try {
    listData.value = await listProjectScenarios(projectId.value);
    const queryId = typeof route.query.id === "string" ? route.query.id : "";
    if (queryId && listData.value.scenarios.some((s) => s.id === queryId)) {
      if (selectedScenarioId.value !== queryId) {
        void openScenarioDrawer(queryId);
      }
    }
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("scen.loadError");
  } finally {
    loading.value = false;
  }
}

async function handleSyncFromRuns(): Promise<void> {
  if (!projectId.value || syncing.value) return;
  syncing.value = true;
  errorMsg.value = null;
  try {
    listData.value = await syncProjectScenarios(projectId.value);
    showSuccess(t("scen.syncSuccess", { count: listData.value.total }));
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("scen.loadError");
  } finally {
    syncing.value = false;
  }
}

async function openScenarioDrawer(scenarioId: string): Promise<void> {
  if (!projectId.value) return;
  selectedScenarioId.value = scenarioId;
  confirmingDelete.value = false;
  loadingDetail.value = true;
  expandedEndpointKeys.value = new Set();
  if (route.query.id !== scenarioId) {
    void router.replace({ query: { ...route.query, id: scenarioId } });
  }
  try {
    const res = await getScenarioDetail(projectId.value, scenarioId);
    detail.value = res;
    hydrateEditForm(res);
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("scen.loadError");
    selectedScenarioId.value = null;
    detail.value = null;
  } finally {
    loadingDetail.value = false;
  }
}

function closeDrawer(): void {
  selectedScenarioId.value = null;
  detail.value = null;
  confirmingDelete.value = false;
  if (route.query.id) {
    const nextQ = { ...route.query };
    delete nextQ.id;
    void router.replace({ query: nextQ });
  }
}

async function persistScenario(silentToast = false): Promise<void> {
  if (!projectId.value || !detail.value || savingDetail.value) return;
  savingDetail.value = true;
  errorMsg.value = null;
  try {
    const tags = editTags.value
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);

    const updated = await updateScenario(projectId.value, detail.value.id, {
      name: editName.value.trim() || detail.value.name,
      priority: editPriority.value,
      status: editStatus.value,
      business_domain: editDomain.value.trim(),
      owner: editOwner.value.trim(),
      tags,
      description: editDescription.value,
      preconditions: editPreconditions.value,
      assertions: editAssertions.value,
      negative_cases: editNegatives.value,
    });

    detail.value = {
      ...detail.value,
      ...updated,
    };
    hydrateEditForm(detail.value);

    // Refresh summary metrics & list in background
    listData.value = await listProjectScenarios(projectId.value).catch(() => listData.value);

    if (!silentToast) {
      showSuccess(t("scen.savedSuccess", { level: updated.effectiveness_level }));
    }
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("scen.loadError");
  } finally {
    savingDetail.value = false;
  }
}

async function handleAddAssertion(): Promise<void> {
  if (!newAssertTitle.value.trim()) return;
  const dbDsnRef =
    activeOperatorMeta.value.needsDbDsn && newAssertDbDsnRef.value.trim()
      ? newAssertDbDsnRef.value.trim()
      : null;
  editAssertions.value = [
    ...editAssertions.value,
    {
      id: genLocalId("asrt"),
      title: newAssertTitle.value.trim(),
      target_endpoint: newAssertEndpoint.value.trim(),
      jsonpath: newAssertJsonpath.value.trim(),
      operator: newAssertOperator.value,
      expected: newAssertExpected.value.trim(),
      note: newAssertNote.value.trim(),
      status: "pending",
      ...(dbDsnRef ? { db_dsn_ref: dbDsnRef } : {}),
    },
  ];
  newAssertTitle.value = "";
  newAssertJsonpath.value = "";
  newAssertExpected.value = "";
  newAssertDbDsnRef.value = "";
  newAssertNote.value = "";
  await persistScenario();
}

async function handleToggleAssertionStatus(idx: number): Promise<void> {
  const item = editAssertions.value[idx];
  if (!item) return;
  item.status = nextAssertionStatus(item.status);
  await persistScenario(true);
}

async function handleRemoveAssertion(idx: number): Promise<void> {
  editAssertions.value = editAssertions.value.filter((_, i) => i !== idx);
  await persistScenario();
}

async function handleAddNegativeCase(): Promise<void> {
  if (!newNegTitle.value.trim()) return;
  editNegatives.value = [
    ...editNegatives.value,
    {
      id: genLocalId("neg"),
      title: newNegTitle.value.trim(),
      category: newNegCategory.value,
      expected_behavior: newNegExpected.value.trim(),
      note: newNegNote.value.trim(),
      status: "pending",
    },
  ];
  newNegTitle.value = "";
  newNegExpected.value = "";
  newNegNote.value = "";
  await persistScenario();
}

async function handleToggleNegativeStatus(idx: number): Promise<void> {
  const item = editNegatives.value[idx];
  if (!item) return;
  item.status = nextNegativeStatus(item.status);
  await persistScenario(true);
}

async function handleRemoveNegativeCase(idx: number): Promise<void> {
  editNegatives.value = editNegatives.value.filter((_, i) => i !== idx);
  await persistScenario();
}

async function handleDeleteScenario(): Promise<void> {
  if (!projectId.value || !detail.value || deletingDetail.value) return;
  deletingDetail.value = true;
  errorMsg.value = null;
  try {
    await deleteScenario(projectId.value, detail.value.id);
    closeDrawer();
    listData.value = await listProjectScenarios(projectId.value);
    showSuccess(t("scen.deletedSuccess"));
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("scen.loadError");
  } finally {
    deletingDetail.value = false;
  }
}

function openCreateModal(): void {
  createName.value = "";
  createSlug.value = "";
  createPriority.value = "P1";
  createDomain.value = "";
  createOwner.value = "";
  createDescription.value = "";
  createPreconditions.value = "";
  createModalOpen.value = true;
}

async function handleCreateScenario(): Promise<void> {
  if (!projectId.value || !createName.value.trim() || creating.value) return;
  creating.value = true;
  errorMsg.value = null;
  try {
    const created = await createScenario(projectId.value, {
      name: createName.value.trim(),
      slug: createSlug.value.trim() || undefined,
      priority: createPriority.value,
      business_domain: createDomain.value.trim(),
      owner: createOwner.value.trim(),
      description: createDescription.value,
      preconditions: createPreconditions.value,
      source: "manual",
    });
    createModalOpen.value = false;
    listData.value = await listProjectScenarios(projectId.value);
    showSuccess(t("scen.createdSuccess", { name: created.name }));
    void openScenarioDrawer(created.id);
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("scen.loadError");
  } finally {
    creating.value = false;
  }
}

function toggleEndpointExpand(key: string): void {
  const next = new Set(expandedEndpointKeys.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  expandedEndpointKeys.value = next;
}

function openTryIt(ep: InventoryEndpoint): void {
  tryItEndpoint.value = {
    method: ep.method,
    path: ep.path,
    host: ep.host,
    summary: ep.summary,
    parameters: ep.parameters,
    requestExample: ep.request_example,
    customHeaders: ep.custom_headers,
  };
  tryItAssertions.value = filterScenarioAssertionsForEndpoint(
    editAssertions.value,
    ep.method,
    ep.path
  );
  tryItOpen.value = true;
}

function prettyJson(val: unknown): string {
  try {
    return JSON.stringify(val, null, 2);
  } catch {
    return String(val);
  }
}

watch(projectId, () => {
  closeDrawer();
  priorityFilter.value = "ALL";
  levelFilter.value = "ALL";
  statusFilter.value = "ALL";
  domainFilter.value = "ALL";
  void loadScenarios();
});

onMounted(async () => {
  await ensureLoaded();
  void loadScenarios();
});
</script>

<template>
  <div class="space-y-5">
    <!-- Top Header & Actions -->
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <ListChecks class="h-5 w-5 text-indigo-600" />
          <h1 class="text-xl font-bold tracking-tight text-slate-900">{{ t("scen.title") }}</h1>
          <span
            v-if="listData"
            class="rounded-full bg-indigo-50 px-2.5 py-0.5 text-xs font-semibold text-indigo-700"
          >
            {{ listData.total }}
          </span>
        </div>
        <p class="mt-1 max-w-3xl text-xs text-slate-500">{{ t("scen.subtitle") }}</p>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="btn-secondary !py-1.5 !text-xs"
          :disabled="loading"
          @click="loadScenarios"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
          <span>{{ t("scen.refreshBtn") }}</span>
        </button>
        <button
          type="button"
          data-testid="scenarios-sync-btn"
          class="btn-secondary !py-1.5 !text-xs"
          :disabled="syncing || !projectId"
          @click="handleSyncFromRuns"
        >
          <Loader2 v-if="syncing" class="h-3.5 w-3.5 animate-spin text-indigo-600" />
          <Sparkles v-else class="h-3.5 w-3.5 text-indigo-600" />
          <span>{{ syncing ? t("scen.syncingBtn") : t("scen.syncBtn") }}</span>
        </button>
        <button
          type="button"
          data-testid="scenarios-new-btn"
          class="btn-primary !py-1.5 !text-xs"
          :disabled="!projectId"
          @click="openCreateModal"
        >
          <Plus class="h-3.5 w-3.5" />
          <span>{{ t("scen.newBtn") }}</span>
        </button>
      </div>
    </div>

    <!-- Alerts -->
    <div
      v-if="errorMsg"
      class="flex items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
    >
      <div class="flex items-center gap-2">
        <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
        <span>{{ errorMsg }}</span>
      </div>
      <button type="button" class="text-rose-500 hover:text-rose-700" @click="errorMsg = null">
        <X class="h-4 w-4" />
      </button>
    </div>

    <div
      v-if="successMsg"
      class="flex items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800"
    >
      <div class="flex items-center gap-2">
        <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-600" />
        <span>{{ successMsg }}</span>
      </div>
      <button type="button" class="text-emerald-500 hover:text-emerald-700" @click="successMsg = null">
        <X class="h-4 w-4" />
      </button>
    </div>

    <!-- Top Effectiveness KPI Strip -->
    <section
      v-if="listData"
      class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4"
      data-testid="scenarios-kpi-strip"
    >
      <!-- Total Scenarios -->
      <div class="card flex flex-col justify-between p-4">
        <span class="text-xs font-medium text-slate-500">{{ t("scen.kpiTotal") }}</span>
        <div class="mt-2 flex items-baseline gap-2">
          <span class="font-mono text-2xl font-bold text-slate-900 tabular-nums">
            {{ listData.total }}
          </span>
        </div>
        <p class="mt-1 text-[11px] text-slate-500">
          {{ t("scen.kpiTotalSub", { domains: availableDomains.length }) }}
        </p>
      </div>

      <!-- P0/P1 Effectiveness Rate (L3/L4 %) -->
      <div
        class="card flex flex-col justify-between border-emerald-200/80 bg-gradient-to-br from-white to-emerald-50/40 p-4"
        data-testid="scenarios-effectiveness-kpi"
      >
        <span class="text-xs font-semibold text-emerald-800">{{ t("scen.kpiEffectiveness") }}</span>
        <div class="mt-2 flex items-baseline gap-2">
          <span class="font-mono text-2xl font-bold text-emerald-700 tabular-nums">
            {{ listData.p0_p1_total > 0 ? `${listData.p0_p1_effective_pct.toFixed(1)}%` : "—" }}
          </span>
          <span
            v-if="listData.p0_p1_total > 0"
            class="rounded-full bg-emerald-100 px-2 py-0.5 font-mono text-[11px] font-semibold text-emerald-800"
          >
            {{ listData.p0_p1_effective_count }}/{{ listData.p0_p1_total }}
          </span>
        </div>
        <p class="mt-1 text-[11px] text-slate-600">
          {{
            listData.p0_p1_total > 0
              ? t("scen.kpiEffectivenessSub", {
                  effective: listData.p0_p1_effective_count,
                  total: listData.p0_p1_total,
                })
              : t("scen.kpiEffectivenessNone")
          }}
        </p>
      </div>

      <!-- Priority Breakdown -->
      <div class="card flex flex-col justify-between p-4">
        <span class="text-xs font-medium text-slate-500">{{ t("scen.kpiPriorityTitle") }}</span>
        <div class="mt-2.5 grid grid-cols-4 gap-1.5">
          <button
            v-for="p in PRIORITIES"
            :key="p"
            type="button"
            class="flex flex-col items-center rounded-lg px-1.5 py-1.5 text-center ring-1 ring-inset transition"
            :class="[
              priorityBadgeClass(p),
              priorityFilter === p ? 'ring-2 ring-slate-900' : 'hover:opacity-80',
            ]"
            @click="priorityFilter = priorityFilter === p ? 'ALL' : p"
          >
            <span class="font-mono text-[10px] font-bold">{{ p }}</span>
            <span class="font-mono text-sm font-bold tabular-nums">
              {{ listData.by_priority?.[p] ?? 0 }}
            </span>
          </button>
        </div>
      </div>

      <!-- Effectiveness Ladder -->
      <div class="card flex flex-col justify-between p-4">
        <span class="text-xs font-medium text-slate-500">{{ t("scen.kpiLadderTitle") }}</span>
        <div class="mt-2.5 grid grid-cols-5 gap-1">
          <button
            v-for="lvl in EFFECTIVENESS_LEVELS"
            :key="lvl"
            type="button"
            :title="t(`scen.levelDesc.${lvl}`)"
            class="flex flex-col items-center rounded-lg px-1 py-1.5 text-center ring-1 ring-inset transition"
            :class="[
              levelBadgeClass(lvl),
              levelFilter === lvl ? 'ring-2 ring-slate-900' : 'hover:opacity-80',
            ]"
            @click="levelFilter = levelFilter === lvl ? 'ALL' : lvl"
          >
            <span class="font-mono text-[10px] font-bold">{{ lvl }}</span>
            <span class="font-mono text-sm font-bold tabular-nums">
              {{ listData.by_effectiveness?.[lvl] ?? 0 }}
            </span>
          </button>
        </div>
      </div>
    </section>

    <!-- Filter Bar -->
    <div
      v-if="scenarios.length > 0"
      class="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm"
    >
      <div class="relative min-w-[220px] flex-1">
        <Search class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
        <input
          v-model="searchQuery"
          data-testid="scenarios-search"
          type="text"
          :placeholder="t('scen.searchPlaceholder')"
          class="w-full rounded-lg border border-slate-300 py-1.5 pl-9 pr-3 text-xs text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
        />
      </div>

      <!-- Priority pills -->
      <div class="flex items-center gap-1 rounded-lg bg-slate-100 p-1">
        <button
          type="button"
          class="rounded-md px-2 py-1 text-xs font-medium transition"
          :class="priorityFilter === 'ALL' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600'"
          @click="priorityFilter = 'ALL'"
        >
          {{ t("scen.filterPriorityAll") }}
        </button>
        <button
          v-for="p in PRIORITIES"
          :key="p"
          type="button"
          class="rounded-md px-2 py-1 font-mono text-xs font-semibold transition"
          :class="priorityFilter === p ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600'"
          @click="priorityFilter = p"
        >
          {{ p }}
        </button>
      </div>

      <!-- Level Select -->
      <select v-model="levelFilter" class="input-field w-auto !py-1.5 text-xs">
        <option value="ALL">{{ t("scen.filterLevelAll") }}</option>
        <option v-for="lvl in EFFECTIVENESS_LEVELS" :key="lvl" :value="lvl">
          {{ t(`scen.level.${lvl}`) }}
        </option>
      </select>

      <!-- Status Select -->
      <select v-model="statusFilter" class="input-field w-auto !py-1.5 text-xs">
        <option value="ALL">{{ t("scen.filterStatusAll") }}</option>
        <option v-for="st in STATUSES" :key="st" :value="st">
          {{ t(`scen.status.${st}`) }}
        </option>
      </select>

      <!-- Domain Select -->
      <select v-if="availableDomains.length > 0" v-model="domainFilter" class="input-field w-auto !py-1.5 text-xs">
        <option value="ALL">{{ t("scen.filterDomainAll") }}</option>
        <option v-for="dom in availableDomains" :key="dom" :value="dom">
          {{ dom }}
        </option>
      </select>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="card py-16 text-center text-sm text-slate-500">
      {{ t("scen.loading") }}
    </div>

    <!-- Empty -->
    <EmptyState
      v-else-if="scenarios.length === 0"
      :title="t('scen.emptyTitle')"
      :description="t('scen.emptyDesc')"
    >
      <template #icon>
        <ListChecks class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <button type="button" class="btn-secondary" :disabled="syncing" @click="handleSyncFromRuns">
          <Sparkles class="h-4 w-4 text-indigo-600" />
          <span>{{ t("scen.syncBtn") }}</span>
        </button>
        <button type="button" class="btn-primary" @click="openCreateModal">
          <Plus class="h-4 w-4" />
          <span>{{ t("scen.newBtn") }}</span>
        </button>
      </template>
    </EmptyState>

    <!-- No Filter Matches -->
    <div
      v-else-if="filteredScenarios.length === 0"
      class="card py-12 text-center text-sm text-slate-500"
    >
      {{ t("scen.noMatches") }}
    </div>

    <!-- Scenarios Grid -->
    <div
      v-else
      class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
      data-testid="scenarios-card-grid"
    >
      <article
        v-for="item in filteredScenarios"
        :key="item.id"
        data-testid="scenario-card"
        class="group flex cursor-pointer flex-col justify-between rounded-xl border bg-white p-4 shadow-sm transition hover:border-indigo-300 hover:shadow"
        :class="
          selectedScenarioId === item.id
            ? 'border-indigo-500 ring-2 ring-indigo-500/20'
            : 'border-slate-200/90'
        "
        @click="openScenarioDrawer(item.id)"
      >
        <div>
          <!-- Top badges row -->
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex flex-wrap items-center gap-1.5">
              <span
                class="inline-flex rounded-md px-2 py-0.5 font-mono text-[11px] font-bold ring-1 ring-inset"
                :class="priorityBadgeClass(item.priority)"
              >
                {{ item.priority }}
              </span>
              <span
                class="inline-flex rounded-md px-2 py-0.5 text-[11px] font-semibold ring-1 ring-inset"
                :class="levelBadgeClass(item.effectiveness_level)"
                :title="t(`scen.levelDesc.${item.effectiveness_level}`)"
              >
                {{ t(`scen.level.${item.effectiveness_level}`) }}
              </span>
            </div>

            <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">
              {{ t(`scen.source.${item.source}`) }}
            </span>
          </div>

          <!-- Title & Slug -->
          <h3 class="mt-2.5 text-sm font-bold text-slate-900 group-hover:text-indigo-700">
            {{ item.name }}
          </h3>
          <p class="mt-0.5 font-mono text-[11px] text-slate-400">{{ item.slug }}</p>

          <!-- Description preview -->
          <p class="mt-2 line-clamp-2 text-xs leading-relaxed text-slate-500">
            {{ item.description || t("scen.noDescription") }}
          </p>

          <!-- Domain, Owner & Tags -->
          <div class="mt-3 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-500">
            <span
              v-if="item.business_domain"
              class="rounded-full bg-indigo-50/70 px-2 py-0.5 font-medium text-indigo-700"
            >
              {{ item.business_domain }}
            </span>
            <span v-if="item.owner" class="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">
              <User class="h-3 w-3 text-slate-400" />
              <span>{{ item.owner }}</span>
            </span>
            <span
              v-for="tag in (item.tags ?? []).slice(0, 2)"
              :key="tag"
              class="inline-flex items-center gap-0.5 rounded-full bg-slate-100 px-2 py-0.5 text-slate-600"
            >
              <Tag class="h-2.5 w-2.5 text-slate-400" />
              <span>{{ tag }}</span>
            </span>
          </div>
        </div>

        <!-- Bottom Stats Strip -->
        <div
          class="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3 font-mono text-[11px] text-slate-600"
        >
          <span>{{ t("scen.metricEndpoints", { count: item.endpoint_count }) }}</span>
          <span :class="item.assertion_count > 0 ? 'font-semibold text-teal-700' : 'text-slate-400'">
            {{ t("scen.metricAssertions", { count: item.assertion_count }) }}
          </span>
          <span
            :class="
              item.negative_cases_covered_count > 0
                ? 'font-semibold text-emerald-700'
                : 'text-slate-400'
            "
          >
            {{
              t("scen.metricNegatives", {
                covered: item.negative_cases_covered_count,
                total: item.negative_case_count,
              })
            }}
          </span>
        </div>
      </article>
    </div>

    <!-- Rich Scenario Detail Slide-Over Drawer -->
    <Teleport to="body">
      <div
        v-if="selectedScenarioId"
        class="fixed inset-0 z-50 flex justify-end bg-slate-950/50 backdrop-blur-sm"
        data-testid="scenario-detail-drawer"
        @click.self="closeDrawer"
      >
        <div class="flex h-full w-full max-w-3xl flex-col bg-white shadow-2xl">
          <!-- Drawer Header -->
          <div class="flex items-start justify-between gap-3 border-b border-slate-200 px-6 py-4">
            <div class="min-w-0">
              <div v-if="detail" class="flex flex-wrap items-center gap-2">
                <span
                  class="inline-flex rounded-md px-2 py-0.5 font-mono text-xs font-bold ring-1 ring-inset"
                  :class="priorityBadgeClass(detail.priority)"
                >
                  {{ detail.priority }}
                </span>
                <span
                  class="inline-flex rounded-md px-2 py-0.5 text-xs font-semibold ring-1 ring-inset"
                  :class="levelBadgeClass(detail.effectiveness_level)"
                >
                  {{ t(`scen.level.${detail.effectiveness_level}`) }}
                </span>
                <h2 class="truncate text-base font-bold text-slate-900">{{ detail.name }}</h2>
              </div>
              <p v-if="detail" class="mt-0.5 font-mono text-xs text-slate-400">
                {{ detail.slug }} · {{ t(`scen.status.${detail.status}`) }}
              </p>
            </div>

            <div class="flex shrink-0 items-center gap-2">
              <template v-if="detail">
                <template v-if="confirmingDelete">
                  <button
                    type="button"
                    class="rounded-lg bg-rose-600 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-rose-700 disabled:opacity-50"
                    :disabled="deletingDetail"
                    @click="handleDeleteScenario"
                  >
                    {{ t("scen.drawer.deleteConfirm") }}
                  </button>
                  <button
                    type="button"
                    class="btn-secondary !py-1.5 !text-xs"
                    @click="confirmingDelete = false"
                  >
                    {{ t("common.cancel") }}
                  </button>
                </template>
                <button
                  v-else
                  type="button"
                  class="rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-100"
                  @click="confirmingDelete = true"
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </template>
              <button
                type="button"
                class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
                @click="closeDrawer"
              >
                <X class="h-5 w-5" />
              </button>
            </div>
          </div>

          <!-- Drawer Loading -->
          <div
            v-if="loadingDetail || !detail"
            class="flex flex-1 items-center justify-center gap-2 text-sm text-slate-500"
          >
            <Loader2 class="h-4 w-4 animate-spin text-indigo-600" />
            <span>{{ t("scen.drawer.loading") }}</span>
          </div>

          <template v-else>
            <!-- Drawer Tabs -->
            <div class="flex items-center gap-1 border-b border-slate-200 bg-slate-50/70 px-6 pt-2">
              <button
                type="button"
                data-testid="drawer-tab-spec"
                class="border-b-2 px-3 py-2 text-xs font-semibold transition"
                :class="
                  drawerTab === 'spec'
                    ? 'border-indigo-600 text-indigo-700'
                    : 'border-transparent text-slate-500 hover:text-slate-800'
                "
                @click="drawerTab = 'spec'"
              >
                {{ t("scen.drawer.tabSpec") }}
              </button>
              <button
                type="button"
                data-testid="drawer-tab-assertions"
                class="border-b-2 px-3 py-2 text-xs font-semibold transition"
                :class="
                  drawerTab === 'assertions'
                    ? 'border-teal-600 text-teal-700'
                    : 'border-transparent text-slate-500 hover:text-slate-800'
                "
                @click="drawerTab = 'assertions'"
              >
                {{ t("scen.drawer.tabAssertions", { count: editAssertions.length }) }}
              </button>
              <button
                type="button"
                data-testid="drawer-tab-negatives"
                class="border-b-2 px-3 py-2 text-xs font-semibold transition"
                :class="
                  drawerTab === 'negatives'
                    ? 'border-emerald-600 text-emerald-700'
                    : 'border-transparent text-slate-500 hover:text-slate-800'
                "
                @click="drawerTab = 'negatives'"
              >
                {{ t("scen.drawer.tabNegatives", { count: editNegatives.length }) }}
              </button>
              <button
                type="button"
                data-testid="drawer-tab-endpoints"
                class="border-b-2 px-3 py-2 text-xs font-semibold transition"
                :class="
                  drawerTab === 'endpoints'
                    ? 'border-indigo-600 text-indigo-700'
                    : 'border-transparent text-slate-500 hover:text-slate-800'
                "
                @click="drawerTab = 'endpoints'"
              >
                {{ t("scen.drawer.tabEndpoints", { count: detail.endpoints.length }) }}
              </button>
            </div>

            <!-- Drawer Body -->
            <div class="flex-1 overflow-y-auto px-6 py-5">
              <!-- Tab 1: Business Spec & Metadata -->
              <form
                v-if="drawerTab === 'spec'"
                class="space-y-4"
                @submit.prevent="persistScenario()"
              >
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div class="sm:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-slate-700">
                      {{ t("scen.form.name") }}
                    </label>
                    <input
                      v-model="editName"
                      type="text"
                      required
                      :placeholder="t('scen.form.namePlaceholder')"
                      class="input-field"
                    />
                  </div>

                  <div>
                    <label class="mb-1 block text-xs font-medium text-slate-700">
                      {{ t("scen.form.priority") }}
                    </label>
                    <select v-model="editPriority" class="input-field text-xs">
                      <option v-for="p in PRIORITIES" :key="p" :value="p">{{ p }}</option>
                    </select>
                  </div>

                  <div>
                    <label class="mb-1 block text-xs font-medium text-slate-700">
                      {{ t("scen.form.status") }}
                    </label>
                    <select v-model="editStatus" class="input-field text-xs">
                      <option v-for="st in STATUSES" :key="st" :value="st">
                        {{ t(`scen.status.${st}`) }}
                      </option>
                    </select>
                  </div>

                  <div>
                    <label class="mb-1 block text-xs font-medium text-slate-700">
                      {{ t("scen.form.domain") }}
                    </label>
                    <input
                      v-model="editDomain"
                      type="text"
                      :placeholder="t('scen.form.domainPlaceholder')"
                      class="input-field text-xs"
                    />
                  </div>

                  <div>
                    <label class="mb-1 block text-xs font-medium text-slate-700">
                      {{ t("scen.form.owner") }}
                    </label>
                    <input
                      v-model="editOwner"
                      type="text"
                      :placeholder="t('scen.form.ownerPlaceholder')"
                      class="input-field text-xs"
                    />
                  </div>

                  <div class="sm:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-slate-700">
                      {{ t("scen.form.tags") }}
                    </label>
                    <input
                      v-model="editTags"
                      type="text"
                      :placeholder="t('scen.form.tagsPlaceholder')"
                      class="input-field text-xs"
                    />
                  </div>

                  <div class="sm:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-slate-700">
                      {{ t("scen.form.description") }}
                    </label>
                    <textarea
                      v-model="editDescription"
                      rows="4"
                      :placeholder="t('scen.form.descriptionPlaceholder')"
                      class="input-field text-xs leading-relaxed"
                    />
                  </div>

                  <div class="sm:col-span-2">
                    <label class="mb-1 block text-xs font-medium text-slate-700">
                      {{ t("scen.form.preconditions") }}
                    </label>
                    <textarea
                      v-model="editPreconditions"
                      rows="3"
                      :placeholder="t('scen.form.preconditionsPlaceholder')"
                      class="input-field text-xs leading-relaxed"
                    />
                  </div>
                </div>

                <div class="flex justify-end border-t border-slate-100 pt-4">
                  <button
                    type="submit"
                    data-testid="scenario-save-btn"
                    class="btn-primary !text-xs"
                    :disabled="savingDetail"
                  >
                    <Loader2 v-if="savingDetail" class="h-3.5 w-3.5 animate-spin" />
                    <Save v-else class="h-3.5 w-3.5" />
                    <span>{{ savingDetail ? t("scen.drawer.saving") : t("scen.drawer.save") }}</span>
                  </button>
                </div>
              </form>

              <!-- Tab 2: Business Assertions Panel -->
              <div v-else-if="drawerTab === 'assertions'" class="space-y-4">
                <div
                  class="flex items-start gap-2.5 rounded-xl border border-teal-200 bg-teal-50/70 px-4 py-3 text-xs text-teal-900"
                >
                  <Code2 class="mt-0.5 h-4 w-4 shrink-0 text-teal-600" />
                  <span>{{ t("scen.assert.compileHint") }}</span>
                </div>

                <!-- Existing Assertions List -->
                <div
                  v-if="editAssertions.length === 0"
                  class="rounded-xl border border-dashed border-slate-200 p-6 text-center text-xs text-slate-500"
                >
                  {{ t("scen.assert.empty") }}
                </div>

                <ul v-else class="space-y-2.5">
                  <li
                    v-for="(asrt, idx) in editAssertions"
                    :key="asrt.id || idx"
                    data-testid="scenario-assertion-row"
                    class="rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm"
                  >
                    <div class="flex items-start justify-between gap-2">
                      <div class="min-w-0 space-y-1">
                        <div class="flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            class="rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ring-inset transition hover:opacity-80"
                            :class="assertionStatusClass(asrt.status)"
                            @click="handleToggleAssertionStatus(idx)"
                          >
                            {{ t(`scen.assert.status.${asrt.status}`) }}
                          </button>
                          <span class="text-xs font-semibold text-slate-900">{{ asrt.title }}</span>
                        </div>
                        <div class="flex flex-wrap items-center gap-2 font-mono text-[11px] text-slate-600">
                          <span
                            v-if="asrt.target_endpoint"
                            class="rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-700"
                          >
                            {{ asrt.target_endpoint }}
                          </span>
                          <span v-if="asrt.jsonpath" class="rounded bg-slate-100 px-1.5 py-0.5 text-slate-800">
                            {{ asrt.jsonpath }}
                          </span>
                          <span
                            class="rounded px-1.5 py-0.5 font-semibold"
                            :class="
                              isSqlOperator(asrt.operator)
                                ? 'bg-violet-50 text-violet-700'
                                : 'bg-teal-50 text-teal-700'
                            "
                          >
                            {{ asrt.operator }}
                          </span>
                          <span v-if="asrt.expected" class="rounded bg-slate-900 px-1.5 py-0.5 text-emerald-300">
                            {{ asrt.expected }}
                          </span>
                          <span
                            v-if="asrt.db_dsn_ref"
                            data-testid="assertion-db-dsn-badge"
                            class="inline-flex items-center gap-1 rounded border border-violet-200 bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700"
                          >
                            <Database class="h-3 w-3 shrink-0" />
                            <span>{{ t("scen.assert.dbBadgePrefix") }}: {{ asrt.db_dsn_ref }}</span>
                          </span>
                        </div>
                        <p v-if="asrt.note" class="text-[11px] text-slate-500">{{ asrt.note }}</p>
                      </div>

                      <button
                        type="button"
                        class="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                        @click="handleRemoveAssertion(idx)"
                      >
                        <Trash2 class="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </li>
                </ul>

                <!-- Add Business Assertion Form -->
                <form
                  class="space-y-3 rounded-xl border border-slate-200 bg-slate-50/70 p-4"
                  @submit.prevent="handleAddAssertion"
                >
                  <h4 class="text-xs font-bold uppercase tracking-wider text-slate-700">
                    {{ t("scen.assert.addTitle") }}
                  </h4>
                  <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <div class="sm:col-span-2">
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{ t("scen.assert.titleLabel") }}
                      </label>
                      <input
                        v-model="newAssertTitle"
                        data-testid="assertion-title-input"
                        type="text"
                        required
                        :placeholder="t('scen.assert.titlePlaceholder')"
                        class="input-field text-xs"
                      />
                    </div>

                    <div class="sm:col-span-2">
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{ t("scen.assert.endpointLabel") }}
                      </label>
                      <select
                        v-if="detail.endpoints.length > 0"
                        v-model="newAssertEndpoint"
                        class="input-field font-mono text-xs"
                      >
                        <option value="">{{ t("scen.assert.endpointAny") }}</option>
                        <option
                          v-for="ep in detail.endpoints"
                          :key="`${ep.method} ${ep.path}`"
                          :value="`${ep.method.toUpperCase()} ${ep.path}`"
                        >
                          {{ ep.method.toUpperCase() }} {{ ep.path }}
                        </option>
                      </select>
                      <input
                        v-else
                        v-model="newAssertEndpoint"
                        type="text"
                        placeholder="POST /api/v1/orders"
                        class="input-field font-mono text-xs"
                      />
                    </div>

                    <div>
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{ t("scen.assert.operatorLabel") }}
                      </label>
                      <select
                        v-model="newAssertOperator"
                        data-testid="assertion-operator-select"
                        class="input-field font-mono text-xs"
                      >
                        <optgroup
                          v-for="group in ASSERTION_OPERATOR_GROUPS"
                          :key="group.id"
                          :label="locale === 'zh' ? group.labelZh : group.labelEn"
                        >
                          <option
                            v-for="op in group.operators"
                            :key="op.value"
                            :value="op.value"
                          >
                            {{ op.value }} — {{ locale === "zh" ? op.labelZh : op.labelEn }}
                          </option>
                        </optgroup>
                      </select>
                    </div>

                    <div>
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{
                          locale === "zh"
                            ? activeOperatorMeta.jsonpathLabelZh
                            : activeOperatorMeta.jsonpathLabelEn
                        }}
                      </label>
                      <input
                        v-model="newAssertJsonpath"
                        data-testid="assertion-jsonpath-input"
                        type="text"
                        :placeholder="activeOperatorMeta.jsonpathPlaceholder"
                        class="input-field font-mono text-xs"
                      />
                    </div>

                    <div>
                      <label class="mb-1 flex items-center gap-1.5 text-xs font-medium text-slate-700">
                        <span>{{ t("scen.assert.expectedLabel") }}</span>
                        <span
                          v-if="!activeOperatorMeta.needsExpected"
                          class="text-[10px] font-normal text-slate-400"
                        >
                          {{ t("scen.assert.expectedOptional") }}
                        </span>
                      </label>
                      <input
                        v-model="newAssertExpected"
                        data-testid="assertion-expected-input"
                        type="text"
                        :placeholder="activeOperatorMeta.expectedPlaceholder"
                        class="input-field font-mono text-xs"
                      />
                    </div>

                    <div>
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{ t("scen.assert.noteLabel") }}
                      </label>
                      <input
                        v-model="newAssertNote"
                        type="text"
                        :placeholder="t('scen.assert.notePlaceholder')"
                        class="input-field text-xs"
                      />
                    </div>

                    <div
                      v-if="activeOperatorMeta.hintZh || activeOperatorMeta.hintEn"
                      class="sm:col-span-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] text-slate-600"
                    >
                      {{ locale === "zh" ? activeOperatorMeta.hintZh : activeOperatorMeta.hintEn }}
                    </div>

                    <div
                      v-if="activeOperatorMeta.needsDbDsn"
                      class="sm:col-span-2 space-y-2 rounded-lg border border-violet-200 bg-violet-50/70 p-3"
                    >
                      <div>
                        <label class="mb-1 flex items-center gap-1.5 text-xs font-medium text-violet-900">
                          <Database class="h-3.5 w-3.5 text-violet-600" />
                          <span>{{ t("scen.assert.dbDsnLabel") }}</span>
                        </label>
                        <input
                          v-model="newAssertDbDsnRef"
                          data-testid="assertion-db-dsn-input"
                          type="text"
                          :placeholder="t('scen.assert.dbDsnPlaceholder')"
                          class="input-field font-mono text-xs"
                        />
                      </div>
                      <p class="text-[11px] leading-relaxed text-violet-800">
                        {{ t("scen.assert.dbReadonlyHint") }}
                      </p>
                    </div>
                  </div>

                  <div class="flex justify-end">
                    <button
                      type="submit"
                      data-testid="assertion-add-btn"
                      class="btn-primary !py-1.5 !text-xs"
                      :disabled="savingDetail || !newAssertTitle.trim()"
                    >
                      <Plus class="h-3.5 w-3.5" />
                      <span>{{ t("scen.assert.addBtn") }}</span>
                    </button>
                  </div>
                </form>
              </div>

              <!-- Tab 3: Negative & Boundary Cases Panel -->
              <div v-else-if="drawerTab === 'negatives'" class="space-y-4">
                <div
                  class="flex items-start gap-2.5 rounded-xl border border-amber-200 bg-amber-50/70 px-4 py-3 text-xs text-amber-900"
                >
                  <ShieldAlert class="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
                  <span>{{ t("scen.neg.hint") }}</span>
                </div>

                <div
                  v-if="editNegatives.length === 0"
                  class="rounded-xl border border-dashed border-slate-200 p-6 text-center text-xs text-slate-500"
                >
                  {{ t("scen.neg.empty") }}
                </div>

                <ul v-else class="space-y-2.5">
                  <li
                    v-for="(nc, idx) in editNegatives"
                    :key="nc.id || idx"
                    data-testid="scenario-negative-row"
                    class="rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm"
                  >
                    <div class="flex items-start justify-between gap-2">
                      <div class="min-w-0 space-y-1">
                        <div class="flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            class="rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ring-inset transition hover:opacity-80"
                            :class="negativeStatusClass(nc.status)"
                            @click="handleToggleNegativeStatus(idx)"
                          >
                            {{ t(`scen.neg.status.${nc.status}`) }}
                          </button>
                          <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-700">
                            {{ t(`scen.neg.cat.${nc.category}`) }}
                          </span>
                          <span class="text-xs font-semibold text-slate-900">{{ nc.title }}</span>
                        </div>
                        <p v-if="nc.expected_behavior" class="text-xs text-slate-600">
                          {{ nc.expected_behavior }}
                        </p>
                        <p v-if="nc.note" class="text-[11px] text-slate-400">{{ nc.note }}</p>
                      </div>
                      <button
                        type="button"
                        class="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                        @click="handleRemoveNegativeCase(idx)"
                      >
                        <Trash2 class="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </li>
                </ul>

                <!-- Add Negative Case Form -->
                <form
                  class="space-y-3 rounded-xl border border-slate-200 bg-slate-50/70 p-4"
                  @submit.prevent="handleAddNegativeCase"
                >
                  <h4 class="text-xs font-bold uppercase tracking-wider text-slate-700">
                    {{ t("scen.neg.addTitle") }}
                  </h4>
                  <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <div class="sm:col-span-2">
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{ t("scen.neg.titleLabel") }}
                      </label>
                      <input
                        v-model="newNegTitle"
                        data-testid="negative-title-input"
                        type="text"
                        required
                        :placeholder="t('scen.neg.titlePlaceholder')"
                        class="input-field text-xs"
                      />
                    </div>
                    <div>
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{ t("scen.neg.categoryLabel") }}
                      </label>
                      <select v-model="newNegCategory" class="input-field text-xs">
                        <option v-for="cat in NEGATIVE_CATEGORIES" :key="cat" :value="cat">
                          {{ t(`scen.neg.cat.${cat}`) }}
                        </option>
                      </select>
                    </div>
                    <div>
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{ t("scen.neg.noteLabel") }}
                      </label>
                      <input v-model="newNegNote" type="text" class="input-field text-xs" />
                    </div>
                    <div class="sm:col-span-2">
                      <label class="mb-1 block text-xs font-medium text-slate-700">
                        {{ t("scen.neg.expectedLabel") }}
                      </label>
                      <input
                        v-model="newNegExpected"
                        data-testid="negative-expected-input"
                        type="text"
                        :placeholder="t('scen.neg.expectedPlaceholder')"
                        class="input-field text-xs"
                      />
                    </div>
                  </div>
                  <div class="flex justify-end">
                    <button
                      type="submit"
                      data-testid="negative-add-btn"
                      class="btn-primary !py-1.5 !text-xs"
                      :disabled="savingDetail || !newNegTitle.trim()"
                    >
                      <Plus class="h-3.5 w-3.5" />
                      <span>{{ t("scen.neg.addBtn") }}</span>
                    </button>
                  </div>
                </form>
              </div>

              <!-- Tab 4: Covered Endpoints & Linked Runs -->
              <div v-else class="space-y-5">
                <div
                  v-if="detail.endpoints.length === 0"
                  class="rounded-xl border border-dashed border-slate-200 p-6 text-center text-xs text-slate-500"
                >
                  {{ t("scen.ep.empty") }}
                </div>

                <div v-else class="space-y-2.5">
                  <div
                    v-for="ep in detail.endpoints"
                    :key="`${ep.method} ${ep.path}`"
                    class="overflow-hidden rounded-xl border border-slate-200 bg-white"
                  >
                    <div
                      class="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2.5 hover:bg-slate-50/70"
                    >
                      <button
                        type="button"
                        class="flex min-w-0 flex-1 items-center gap-2 text-left"
                        @click="toggleEndpointExpand(`${ep.method} ${ep.path}`)"
                      >
                        <ChevronDown
                          v-if="expandedEndpointKeys.has(`${ep.method} ${ep.path}`)"
                          class="h-3.5 w-3.5 shrink-0 text-indigo-600"
                        />
                        <ChevronRight v-else class="h-3.5 w-3.5 shrink-0 text-slate-400" />
                        <span
                          class="inline-flex shrink-0 rounded px-1.5 py-0.5 font-mono text-[11px] font-bold ring-1 ring-inset"
                          :class="methodBadgeClass(ep.method)"
                        >
                          {{ ep.method.toUpperCase() }}
                        </span>
                        <span class="truncate font-mono text-xs font-medium text-slate-900">
                          {{ ep.path }}
                        </span>
                        <StatusBadge :status="ep.discovery_source" size="sm" />
                      </button>

                      <button
                        type="button"
                        data-testid="scenario-endpoint-try-it"
                        class="inline-flex shrink-0 items-center gap-1 rounded-md border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[11px] font-semibold text-indigo-700 hover:bg-indigo-100"
                        @click="openTryIt(ep)"
                      >
                        <Play class="h-3 w-3" />
                        <span>{{ t("inv.tryItBtn") }}</span>
                      </button>
                    </div>

                    <div
                      v-if="expandedEndpointKeys.has(`${ep.method} ${ep.path}`)"
                      class="border-t border-slate-100 bg-slate-50/60 p-3.5"
                    >
                      <p class="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                        {{ t("scen.ep.exampleToggle") }}
                      </p>
                      <pre
                        v-if="ep.response_example !== undefined && ep.response_example !== null"
                        class="max-h-56 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-emerald-200"
                      >{{ prettyJson(ep.response_example) }}</pre>
                      <p v-else class="text-xs text-slate-400">{{ t("inv.noRespBody") }}</p>
                    </div>
                  </div>
                </div>

                <!-- Linked Runs -->
                <div class="rounded-xl border border-slate-200 bg-slate-50/50 p-4">
                  <h4 class="text-xs font-semibold uppercase tracking-wider text-slate-600">
                    {{ t("scen.ep.linkedRuns", { count: detail.runs.length }) }}
                  </h4>
                  <p v-if="detail.runs.length === 0" class="mt-2 text-xs text-slate-400">
                    {{ t("scen.ep.noRuns") }}
                  </p>
                  <div v-else class="mt-2.5 flex flex-wrap gap-2">
                    <router-link
                      v-for="r in detail.runs"
                      :key="r.run_id"
                      :to="`/p/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(r.run_id)}`"
                      class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-indigo-700 shadow-sm hover:border-indigo-300 hover:bg-indigo-50/40"
                    >
                      <span>{{ r.run_name || r.run_id.slice(0, 8) }}</span>
                      <StatusBadge :status="r.status" size="sm" />
                      <ExternalLink class="h-3 w-3 text-slate-400" />
                    </router-link>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </Teleport>

    <!-- Create Scenario Modal -->
    <Teleport to="body">
      <div
        v-if="createModalOpen"
        class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4 backdrop-blur-sm"
        data-testid="create-scenario-modal"
        @click.self="createModalOpen = false"
      >
        <div class="w-full max-w-lg overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
          <div class="flex items-center justify-between border-b border-slate-200 px-5 py-4">
            <div>
              <h3 class="text-sm font-bold text-slate-900">{{ t("scen.modal.title") }}</h3>
              <p class="mt-0.5 text-xs text-slate-500">{{ t("scen.modal.subtitle") }}</p>
            </div>
            <button
              type="button"
              class="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              @click="createModalOpen = false"
            >
              <X class="h-4 w-4" />
            </button>
          </div>

          <form class="space-y-3.5 p-5" @submit.prevent="handleCreateScenario">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("scen.form.name") }}
              </label>
              <input
                v-model="createName"
                type="text"
                required
                :placeholder="t('scen.form.namePlaceholder')"
                class="input-field"
              />
            </div>

            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <label class="mb-1 block text-xs font-medium text-slate-700">
                  {{ t("scen.form.slug") }}
                </label>
                <input
                  v-model="createSlug"
                  type="text"
                  :placeholder="t('scen.form.slugPlaceholder')"
                  class="input-field font-mono text-xs"
                />
              </div>
              <div>
                <label class="mb-1 block text-xs font-medium text-slate-700">
                  {{ t("scen.form.priority") }}
                </label>
                <select v-model="createPriority" class="input-field text-xs">
                  <option v-for="p in PRIORITIES" :key="p" :value="p">{{ p }}</option>
                </select>
              </div>
              <div>
                <label class="mb-1 block text-xs font-medium text-slate-700">
                  {{ t("scen.form.domain") }}
                </label>
                <input
                  v-model="createDomain"
                  type="text"
                  :placeholder="t('scen.form.domainPlaceholder')"
                  class="input-field text-xs"
                />
              </div>
              <div>
                <label class="mb-1 block text-xs font-medium text-slate-700">
                  {{ t("scen.form.owner") }}
                </label>
                <input
                  v-model="createOwner"
                  type="text"
                  :placeholder="t('scen.form.ownerPlaceholder')"
                  class="input-field text-xs"
                />
              </div>
            </div>

            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("scen.form.description") }}
              </label>
              <textarea
                v-model="createDescription"
                rows="3"
                :placeholder="t('scen.form.descriptionPlaceholder')"
                class="input-field text-xs"
              />
            </div>

            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("scen.form.preconditions") }}
              </label>
              <textarea
                v-model="createPreconditions"
                rows="2"
                :placeholder="t('scen.form.preconditionsPlaceholder')"
                class="input-field text-xs"
              />
            </div>

            <div class="flex justify-end gap-2 pt-2">
              <button type="button" class="btn-secondary" @click="createModalOpen = false">
                {{ t("common.cancel") }}
              </button>
              <button
                type="submit"
                class="btn-primary"
                :disabled="creating || !createName.trim()"
              >
                {{ creating ? t("scen.modal.creating") : t("scen.modal.submit") }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <!-- Reusable Inline Try It Drawer -->
    <TryItDrawer
      :open="tryItOpen"
      :project-id="projectId"
      :endpoint="tryItEndpoint"
      :assertions="tryItAssertions"
      @close="tryItOpen = false"
    />
  </div>
</template>
