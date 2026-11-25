<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  Code2,
  Compass,
  Download,
  FileText,
  FileUp,
  FolderKanban,
  Gauge,
  GitBranch,
  Globe,
  HelpCircle,
  Loader2,
  RefreshCw,
  Settings,
  ShieldCheck,
  Wrench,
  X,
  XCircle,
} from "lucide-vue-next";
import { getProjectInventory, listKnowledgeDocs, listTargets } from "@/api/projects";
import { listProjectScenarios } from "@/api/scenarios";
import { getRunTitle, listProjectRuns, shortRunId } from "@/api/runs";
import {
  downloadAcceptanceReport,
  downloadPostmanCollection,
  getDoctorStatus,
} from "@/api/reports";
import type {
  DoctorToolStatus,
  KnowledgeDoc,
  ProjectInventoryResponse,
  Run,
  ScenarioListResponse,
  Target,
} from "@/api/types";
import { useWorkspace } from "@/stores/workspace";
import { useI18n } from "@/i18n";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import MetricCard from "@/components/layout/MetricCard.vue";
import EmptyState from "@/components/layout/EmptyState.vue";
import AcceptancePromptModal from "@/components/workspace/AcceptancePromptModal.vue";

interface DoctorEntry extends DoctorToolStatus {
  name: string;
}

const CORE_TOOL_ORDER = ["mvn", "allure", "k6", "mitmdump", "nuclei", "java", "docker"];
const CORE_TOOL_SET = new Set(CORE_TOOL_ORDER);

const route = useRoute();
const { locale, t } = useI18n();
const { activeProject, activeProjectId, ensureLoaded, workspacePath } = useWorkspace();

const projectId = computed(
  () =>
    (typeof route.params.projectId === "string" && route.params.projectId) ||
    activeProjectId.value ||
    ""
);

const loading = ref(true);
const doctorLoading = ref(true);
const error = ref<string | null>(null);
const exportingKind = ref<"html" | "markdown" | "postman" | null>(null);
const exportError = ref<string | null>(null);
const acceptancePromptOpen = ref(false);
const pillarsManualToggle = ref<boolean | null>(null);

async function handleExportDeliverable(kind: "html" | "markdown" | "postman"): Promise<void> {
  if (!projectId.value || exportingKind.value) return;
  exportingKind.value = kind;
  exportError.value = null;
  try {
    if (kind === "postman") {
      await downloadPostmanCollection(projectId.value);
    } else {
      await downloadAcceptanceReport(projectId.value, { format: kind });
    }
  } catch (err) {
    exportError.value = err instanceof Error ? err.message : t("projOverview.exportError");
  } finally {
    exportingKind.value = null;
  }
}

const showPillars = computed(() => {
  if (pillarsManualToggle.value !== null) return pillarsManualToggle.value;
  return runs.value.length === 0;
});

function togglePillars(): void {
  pillarsManualToggle.value = !showPillars.value;
}

const inventory = ref<ProjectInventoryResponse | null>(null);
const scenarioSummary = ref<ScenarioListResponse | null>(null);
const runs = ref<Run[]>([]);
const targets = ref<Target[]>([]);
const knowledgeDocs = ref<KnowledgeDoc[]>([]);
const doctorTools = ref<Record<string, DoctorToolStatus>>({});

const portalTargetBadges = computed(() =>
  targets.value.map((tItem) => {
    const rawLabel = tItem.scope_rules?.portal_label;
    const label = typeof rawLabel === "string" ? rawLabel.trim() : "";
    let host = tItem.root_domain;
    try {
      host = new URL(tItem.base_url).host;
    } catch {
      // fallback to root_domain
    }
    return { id: tItem.id, label, host, baseUrl: tItem.base_url };
  })
);

const totalEndpoints = computed(() => {
  if (inventory.value) return inventory.value.total_endpoints;
  const latest = runs.value[0]?.summary_metrics?.total_endpoints;
  return typeof latest === "number" ? latest : 0;
});

const liveEndpoints = computed(() => inventory.value?.har_live_count ?? 0);
const staticEndpoints = computed(() => inventory.value?.js_static_count ?? 0);

const coveragePctDisplay = computed(() => {
  if (inventory.value && inventory.value.total_endpoints > 0) {
    return `${inventory.value.coverage_pct.toFixed(1)}%`;
  }
  const latest = runs.value[0]?.summary_metrics?.coverage_pct;
  return typeof latest === "number" ? `${latest.toFixed(1)}%` : "—";
});

const completedRunsCount = computed(
  () => runs.value.filter((r) => r.status === "completed").length
);
const activeRunsCount = computed(
  () => runs.value.filter((r) => r.status === "running" || r.status === "queued").length
);

const effectivenessDisplay = computed(() => {
  const s = scenarioSummary.value;
  if (!s || s.p0_p1_total === 0) return "—";
  return `${s.p0_p1_effective_pct.toFixed(0)}%`;
});

const effectivenessSubtitle = computed(() => {
  const s = scenarioSummary.value;
  if (!s) return t("projOverview.kpi.effectivenessEmpty", { all: 0 });
  if (s.p0_p1_total === 0) {
    return t("projOverview.kpi.effectivenessEmpty", { all: s.total });
  }
  return t("projOverview.kpi.effectivenessSub", {
    effective: s.p0_p1_effective_count,
    total: s.p0_p1_total,
    all: s.total,
  });
});

const doctorEntries = computed<DoctorEntry[]>(() =>
  Object.entries(doctorTools.value).map(([name, info]) => ({
    name,
    ...info,
  }))
);

const coreExecutionTools = computed<DoctorEntry[]>(() => {
  const list = doctorEntries.value.filter((item) => CORE_TOOL_SET.has(item.name));
  list.sort((a, b) => CORE_TOOL_ORDER.indexOf(a.name) - CORE_TOOL_ORDER.indexOf(b.name));
  return list;
});

const coreAvailableCount = computed(
  () => coreExecutionTools.value.filter((item) => item.available).length
);

const totalAvailableCount = computed(
  () => doctorEntries.value.filter((item) => item.available).length
);

const corePillars = computed(() => [
  {
    key: "java",
    title: t("overview.pillar.javaTitle"),
    badge: t("overview.pillar.javaBadge"),
    desc: t("overview.pillar.javaDesc"),
    icon: Code2,
    accent: "text-indigo-300 bg-indigo-500/15 ring-indigo-400/30",
  },
  {
    key: "perf",
    title: t("overview.pillar.perfTitle"),
    badge: t("overview.pillar.perfBadge"),
    desc: t("overview.pillar.perfDesc"),
    icon: Gauge,
    accent: "text-emerald-300 bg-emerald-500/15 ring-emerald-400/30",
  },
  {
    key: "sec",
    title: t("overview.pillar.secTitle"),
    badge: t("overview.pillar.secBadge"),
    desc: t("overview.pillar.secDesc"),
    icon: ShieldCheck,
    accent: "text-amber-300 bg-amber-500/15 ring-amber-400/30",
  },
]);

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(locale.value === "zh" ? "zh-CN" : "en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function getRunModules(run: Run): string[] {
  if (run.jobs && run.jobs.length > 0) {
    return run.jobs.map((j) => j.module);
  }
  const snap = run.config_snapshot?.modules;
  return Array.isArray(snap) ? snap.map(String) : [];
}

async function loadProjectWorkspace(pid: string): Promise<void> {
  if (!pid) {
    inventory.value = null;
    scenarioSummary.value = null;
    runs.value = [];
    targets.value = [];
    knowledgeDocs.value = [];
    loading.value = false;
    return;
  }

  loading.value = true;
  error.value = null;
  try {
    const [invRes, scenRes, runsRes, targetsRes, kbRes] = await Promise.all([
      getProjectInventory(pid).catch(() => null),
      listProjectScenarios(pid).catch(() => null),
      listProjectRuns(pid).catch(() => [] as Run[]),
      listTargets(pid).catch(() => [] as Target[]),
      listKnowledgeDocs(pid).catch(() => [] as KnowledgeDoc[]),
    ]);

    if (projectId.value !== pid) return;

    inventory.value = invRes;
    scenarioSummary.value = scenRes;
    runs.value = [...runsRes].sort((a, b) => {
      const ta = a.started_at ? new Date(a.started_at).getTime() : 0;
      const tb = b.started_at ? new Date(b.started_at).getTime() : 0;
      return tb - ta;
    });
    targets.value = targetsRes;
    knowledgeDocs.value = kbRes;
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("overview.err.load");
  } finally {
    loading.value = false;
  }
}

async function loadDoctorSummary(): Promise<void> {
  doctorLoading.value = true;
  try {
    doctorTools.value = await getDoctorStatus();
  } catch {
    doctorTools.value = {};
  } finally {
    doctorLoading.value = false;
  }
}

async function refreshAll(): Promise<void> {
  await Promise.all([loadProjectWorkspace(projectId.value), loadDoctorSummary()]);
}

watch(projectId, (pid) => {
  pillarsManualToggle.value = null;
  void loadProjectWorkspace(pid);
});

onMounted(async () => {
  await ensureLoaded();
  void refreshAll();
});
</script>

<template>
  <div class="space-y-6">
    <!-- No Active Project Fallback -->
    <EmptyState
      v-if="!projectId && !loading"
      :title="t('projOverview.noProjectTitle')"
      :description="t('projOverview.noProjectDesc')"
    >
      <template #icon>
        <FolderKanban class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <router-link to="/projects" class="btn-primary">
          <span>{{ t("projOverview.goProjects") }}</span>
          <ArrowRight class="h-4 w-4" />
        </router-link>
      </template>
    </EmptyState>

    <template v-else>
      <!-- Active Project Hero Banner -->
      <section
        class="relative overflow-hidden rounded-2xl border border-slate-800 bg-gradient-to-br from-slate-900 via-slate-900 to-indigo-950 p-6 text-white shadow-md"
        data-testid="project-overview-hero"
      >
        <!-- Top Row: Clean Title on Left, 3 Focused Primary Actions + How It Works toggle on Right -->
        <div class="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div class="min-w-0 flex-1">
            <h2 class="truncate text-2xl font-bold tracking-tight text-white">
              {{ activeProject?.name || t("overview.heroTitle") }}
            </h2>
          </div>

          <div class="flex shrink-0 flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="ai-acceptance-prompt-btn"
              class="inline-flex items-center gap-2 whitespace-nowrap rounded-lg bg-gradient-to-r from-violet-500 to-indigo-500 px-4 py-2 text-sm font-semibold text-white shadow-sm ring-1 ring-inset ring-white/20 transition hover:from-violet-400 hover:to-indigo-400"
              @click="acceptancePromptOpen = true"
            >
              <Bot class="h-4 w-4 shrink-0" />
              <span>{{ t("acceptPrompt.btn") }}</span>
            </button>
            <router-link
              :to="{ path: workspacePath('runs', projectId), query: { new: '1' } }"
              class="inline-flex items-center gap-2 whitespace-nowrap rounded-lg bg-indigo-500 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-400"
              data-testid="overview-new-run-cta"
            >
              <FileUp class="h-4 w-4 shrink-0" />
              <span>{{ t("overview.ctaImport") }}</span>
            </router-link>
            <router-link
              :to="workspacePath('explore', projectId)"
              class="inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-sm font-medium text-slate-200 transition hover:border-sky-400/50 hover:bg-slate-800 hover:text-white"
              data-testid="overview-explore-cta"
            >
              <Compass class="h-4 w-4 shrink-0 text-sky-300" />
              <span>{{ t("overview.ctaExplore") }}</span>
            </router-link>
            <button
              type="button"
              data-testid="overview-pillars-toggle"
              class="inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg border px-2.5 py-2 text-xs font-medium transition"
              :class="
                showPillars
                  ? 'border-indigo-400/50 bg-indigo-500/20 text-indigo-100'
                  : 'border-slate-700 bg-slate-800/80 text-slate-300 hover:bg-slate-700 hover:text-white'
              "
              @click="togglePillars"
            >
              <HelpCircle class="h-3.5 w-3.5 shrink-0" />
              <span>{{ t("projOverview.howItWorks") }}</span>
            </button>
            <router-link
              :to="workspacePath('settings', projectId)"
              class="inline-flex items-center justify-center rounded-lg border border-slate-700 bg-slate-800/80 p-2 text-slate-300 transition hover:bg-slate-700 hover:text-white"
              :title="t('projOverview.quickSettings')"
            >
              <Settings class="h-4 w-4" />
            </router-link>
          </div>
        </div>

        <!-- Clean Full-Width Portal Bar (never squished!) -->
        <div
          v-if="portalTargetBadges.length > 0"
          data-testid="overview-portal-targets"
          class="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-800/70 pt-3.5"
        >
          <span
            class="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full bg-indigo-500/20 px-2.5 py-1 text-[11px] font-semibold text-indigo-200 ring-1 ring-inset ring-indigo-400/30"
          >
            <Globe class="h-3 w-3 shrink-0" />
            <span>{{ t("projOverview.portalsCount", { count: portalTargetBadges.length }) }}</span>
          </span>
          <a
            v-for="portal in portalTargetBadges.slice(0, 4)"
            :key="portal.id"
            :href="portal.baseUrl"
            target="_blank"
            rel="noopener noreferrer"
            :title="portal.baseUrl"
            class="inline-flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-full bg-slate-800/90 px-3 py-1 text-xs text-slate-200 ring-1 ring-inset ring-slate-700/90 transition hover:border-sky-400/50 hover:bg-slate-800 hover:text-white"
          >
            <span v-if="portal.label" class="shrink-0 font-semibold text-white">{{ portal.label }}</span>
            <span v-if="portal.label" class="text-slate-500">·</span>
            <span class="max-w-[200px] truncate font-mono text-[11px] text-sky-300">
              {{ portal.host }}
            </span>
          </a>
          <router-link
            v-if="portalTargetBadges.length > 4"
            :to="{ path: workspacePath('settings', projectId), query: { tab: 'targets' } }"
            class="inline-flex shrink-0 items-center whitespace-nowrap rounded-full bg-slate-800/70 px-2.5 py-1 text-[11px] font-medium text-indigo-300 ring-1 ring-inset ring-slate-700 hover:bg-slate-700"
          >
            {{ t("projOverview.morePortals", { count: portalTargetBadges.length - 4 }) }}
          </router-link>
        </div>

        <!-- Visual User Journey: Input -> Hub -> 3 Core Capabilities (auto-collapsed once runs > 0) -->
        <div
          v-if="showPillars"
          data-testid="overview-pillars-panel"
          class="mt-5 grid grid-cols-1 gap-3 border-t border-slate-800/80 pt-5 lg:grid-cols-12"
        >
          <div class="rounded-xl border border-slate-700/80 bg-slate-800/60 p-4 lg:col-span-3">
            <div class="flex items-center justify-between">
              <span class="font-mono text-[10px] font-bold uppercase tracking-wider text-sky-300">
                {{ t("overview.stage.inputTag") }}
              </span>
              <FileUp class="h-4 w-4 text-sky-300" />
            </div>
            <h3 class="mt-1.5 text-sm font-semibold text-white">
              {{ t("overview.stage.inputTitle") }}
            </h3>
            <p class="mt-1 text-xs leading-relaxed text-slate-400">
              {{ t("overview.stage.inputDesc") }}
            </p>
          </div>

          <div class="rounded-xl border border-indigo-500/40 bg-indigo-950/50 p-4 lg:col-span-3">
            <div class="flex items-center justify-between">
              <span class="font-mono text-[10px] font-bold uppercase tracking-wider text-indigo-300">
                {{ t("overview.stage.hubTag") }}
              </span>
              <GitBranch class="h-4 w-4 text-indigo-300" />
            </div>
            <h3 class="mt-1.5 text-sm font-semibold text-white">
              {{ t("overview.stage.hubTitle") }}
            </h3>
            <p class="mt-1 text-xs leading-relaxed text-slate-300">
              {{ t("overview.stage.hubDesc") }}
            </p>
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-3 lg:col-span-6">
            <div
              v-for="pillar in corePillars"
              :key="pillar.key"
              class="flex flex-col justify-between rounded-xl border border-slate-700/80 bg-slate-800/45 p-3.5"
            >
              <div>
                <div class="flex items-center justify-between gap-1.5">
                  <span
                    class="inline-flex h-7 w-7 items-center justify-center rounded-lg ring-1 ring-inset"
                    :class="pillar.accent"
                  >
                    <component :is="pillar.icon" class="h-3.5 w-3.5" />
                  </span>
                  <span class="font-mono text-[10px] text-slate-400">{{ pillar.badge }}</span>
                </div>
                <h3 class="mt-2.5 text-xs font-semibold text-white">{{ pillar.title }}</h3>
                <p class="mt-1 text-[11px] leading-relaxed text-slate-400">{{ pillar.desc }}</p>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- Error Banner -->
      <div
        v-if="error"
        class="flex items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
      >
        <div class="flex items-center gap-2">
          <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
          <span>{{ error }}</span>
        </div>
        <button type="button" class="btn-secondary !py-1 !text-xs" @click="refreshAll">
          {{ t("common.retry") }}
        </button>
      </div>

      <!-- Active Project KPI Cards (Coverage + Effectiveness + Runs + KB) -->
      <section class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5" data-testid="project-overview-kpis">
        <MetricCard
          :label="t('projOverview.kpi.inventory')"
          :value="totalEndpoints"
          :subtitle="t('projOverview.kpi.inventorySub', { live: liveEndpoints, static: staticEndpoints })"
          tone="indigo"
        />
        <MetricCard
          :label="t('projOverview.kpi.coverage')"
          :value="coveragePctDisplay"
          :subtitle="
            t('projOverview.kpi.coverageSub', {
              runs: inventory?.runs_merged_count ?? completedRunsCount,
              explores: inventory?.explore_sessions_count ?? 0,
            })
          "
          tone="emerald"
        />
        <router-link
          :to="workspacePath('scenarios', projectId)"
          class="block rounded-xl transition hover:ring-2 hover:ring-indigo-400/40"
          data-testid="overview-effectiveness-kpi"
        >
          <MetricCard
            :label="t('projOverview.kpi.effectiveness')"
            :value="effectivenessDisplay"
            :subtitle="effectivenessSubtitle"
            tone="emerald"
          />
        </router-link>
        <MetricCard
          :label="t('projOverview.kpi.runs')"
          :value="runs.length"
          :subtitle="
            t('projOverview.kpi.runsSub', {
              completed: completedRunsCount,
              active: activeRunsCount,
            })
          "
          tone="neutral"
        />
        <MetricCard
          :label="t('projOverview.kpi.kb')"
          :value="knowledgeDocs.length"
          :subtitle="t('projOverview.kpi.kbSub')"
          tone="amber"
        />
      </section>

      <!-- Active Project Recent Runs Table -->
      <section class="card overflow-hidden">
        <div class="card-header">
          <div>
            <h3 class="text-sm font-semibold text-slate-900">{{ t("overview.recent.title") }}</h3>
            <p class="text-xs text-slate-500">
              {{ t("overview.recent.subtitle") }}
            </p>
          </div>
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="btn-secondary !px-2.5 !py-1.5 !text-xs"
              :disabled="loading"
              @click="loadProjectWorkspace(projectId)"
            >
              <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
              <span>{{ t("common.refresh") }}</span>
            </button>
            <router-link
              :to="workspacePath('runs', projectId)"
              class="btn-secondary !px-2.5 !py-1.5 !text-xs"
            >
              <span>{{ t("overview.recent.allRuns") }}</span>
              <ArrowRight class="h-3.5 w-3.5" />
            </router-link>
          </div>
        </div>

        <div v-if="loading" class="p-6 text-center text-sm text-slate-500">
          {{ t("overview.recent.loading") }}
        </div>

        <div v-else-if="runs.length === 0" class="p-6">
          <EmptyState
            :title="t('overview.recent.emptyTitle')"
            :description="t('overview.recent.emptyDesc')"
          >
            <template #action>
              <router-link :to="workspacePath('explore', projectId)" class="btn-secondary">
                {{ t("overview.ctaExplore") }}
              </router-link>
              <router-link
                :to="{ path: workspacePath('runs', projectId), query: { new: '1' } }"
                class="btn-primary"
              >
                {{ t("overview.ctaImport") }}
              </router-link>
            </template>
          </EmptyState>
        </div>

        <div v-else class="overflow-x-auto">
          <table class="min-w-full divide-y divide-slate-200 text-sm">
            <thead class="table-head">
              <tr>
                <th class="px-5 py-3">{{ t("overview.table.runId") }}</th>
                <th class="px-4 py-3">{{ t("overview.table.status") }}</th>
                <th class="px-4 py-3">{{ t("overview.table.capabilities") }}</th>
                <th class="px-4 py-3 text-right">{{ t("overview.table.endpoints") }}</th>
                <th class="px-4 py-3 text-right">{{ t("overview.table.coverage") }}</th>
                <th class="px-4 py-3 text-right">{{ t("overview.table.findings") }}</th>
                <th class="px-4 py-3">{{ t("overview.table.started") }}</th>
                <th class="px-5 py-3 text-right">{{ t("overview.table.inspect") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 bg-white">
              <tr
                v-for="run in runs.slice(0, 8)"
                :key="run.id"
                class="transition hover:bg-slate-50/80"
              >
                <td class="px-5 py-3">
                  <router-link
                    :to="`/p/${projectId}/runs/${run.id}`"
                    class="block max-w-[260px] truncate text-xs font-semibold text-slate-900 hover:text-indigo-700 hover:underline"
                    :title="getRunTitle(run, activeProject?.name)"
                  >
                    {{ getRunTitle(run, activeProject?.name) }}
                  </router-link>
                  <span class="font-mono text-[10px] text-slate-400" :title="run.id">
                    {{ shortRunId(run.id) }}
                  </span>
                </td>
                <td class="whitespace-nowrap px-4 py-3">
                  <StatusBadge :status="run.status" />
                </td>
                <td class="px-4 py-3">
                  <div class="flex flex-wrap gap-1">
                    <span
                      v-for="mod in getRunModules(run)"
                      :key="mod"
                      class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-medium text-slate-600"
                    >
                      {{ mod }}
                    </span>
                  </div>
                </td>
                <td class="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-slate-700">
                  {{ run.summary_metrics?.total_endpoints ?? "—" }}
                </td>
                <td class="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-slate-700">
                  {{
                    typeof run.summary_metrics?.coverage_pct === "number"
                      ? `${run.summary_metrics.coverage_pct.toFixed(1)}%`
                      : "—"
                  }}
                </td>
                <td class="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-slate-700">
                  {{ run.summary_metrics?.findings_total ?? 0 }}
                </td>
                <td class="whitespace-nowrap px-4 py-3 text-xs text-slate-500">
                  {{ formatDate(run.started_at) }}
                </td>
                <td class="whitespace-nowrap px-5 py-3 text-right">
                  <div class="inline-flex items-center gap-2 text-xs font-medium">
                    <router-link
                      :to="`/p/${projectId}/runs/${run.id}`"
                      class="text-indigo-600 hover:text-indigo-800 hover:underline"
                    >
                      {{ t("common.open") }}
                    </router-link>
                    <span class="text-slate-300">·</span>
                    <router-link
                      :to="{ path: `/p/${projectId}/runs/${run.id}`, query: { tab: 'java' } }"
                      class="text-slate-600 hover:text-slate-900 hover:underline"
                    >
                      {{ t("overview.link.api") }}
                    </router-link>
                    <span class="text-slate-300">·</span>
                    <router-link
                      :to="{ path: `/p/${projectId}/runs/${run.id}`, query: { tab: 'performance' } }"
                      class="text-slate-600 hover:text-slate-900 hover:underline"
                    >
                      {{ t("overview.link.perf") }}
                    </router-link>
                    <span class="text-slate-300">·</span>
                    <router-link
                      :to="{ path: `/p/${projectId}/runs/${run.id}`, query: { tab: 'security' } }"
                      class="text-slate-600 hover:text-slate-900 hover:underline"
                    >
                      {{ t("overview.link.sec") }}
                    </router-link>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- Compact Deliverable Exports Strip (R4) -->
      <section class="card px-5 py-4" data-testid="overview-exports-strip">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
              <FileText class="h-4 w-4" />
            </div>
            <div>
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("projOverview.exportsTitle") }}
              </h3>
              <p class="text-xs text-slate-500">
                {{ t("projOverview.exportsDesc") }}
              </p>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="overview-export-html-btn"
              class="btn-secondary !px-3 !py-1.5 !text-xs"
              :disabled="Boolean(exportingKind)"
              @click="handleExportDeliverable('html')"
            >
              <Loader2 v-if="exportingKind === 'html'" class="h-3.5 w-3.5 animate-spin" />
              <FileText v-else class="h-3.5 w-3.5" />
              <span>
                {{ exportingKind === "html" ? t("projOverview.exporting") : t("projOverview.exportHtml") }}
              </span>
            </button>
            <button
              type="button"
              data-testid="overview-export-md-btn"
              class="btn-secondary !px-3 !py-1.5 !text-xs"
              :disabled="Boolean(exportingKind)"
              @click="handleExportDeliverable('markdown')"
            >
              <Loader2 v-if="exportingKind === 'markdown'" class="h-3.5 w-3.5 animate-spin" />
              <FileText v-else class="h-3.5 w-3.5" />
              <span>
                {{ exportingKind === "markdown" ? t("projOverview.exporting") : t("projOverview.exportMd") }}
              </span>
            </button>
            <button
              type="button"
              data-testid="overview-export-postman-btn"
              class="btn-secondary !px-3 !py-1.5 !text-xs"
              :disabled="Boolean(exportingKind)"
              @click="handleExportDeliverable('postman')"
            >
              <Loader2 v-if="exportingKind === 'postman'" class="h-3.5 w-3.5 animate-spin" />
              <Download v-else class="h-3.5 w-3.5" />
              <span>
                {{
                  exportingKind === "postman"
                    ? t("projOverview.exporting")
                    : t("projOverview.exportPostman")
                }}
              </span>
            </button>
          </div>
        </div>

        <div
          v-if="exportError"
          data-testid="overview-export-error"
          class="mt-3 flex items-center justify-between gap-3 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2 text-xs text-rose-800"
        >
          <div class="flex items-center gap-2">
            <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
            <span>{{ exportError }}</span>
          </div>
          <button
            type="button"
            class="rounded p-0.5 text-rose-600 hover:bg-rose-100"
            @click="exportError = null"
          >
            <X class="h-3.5 w-3.5" />
          </button>
        </div>
      </section>

      <!-- Compact Platform Toolchain Status Strip (Full Doctor lives at /system/doctor) -->
      <section class="card px-5 py-4" data-testid="overview-toolchain-strip">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
              <Wrench class="h-4 w-4" />
            </div>
            <div>
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("projOverview.toolchainCardTitle") }}
              </h3>
              <p class="text-xs text-slate-500">
                <template v-if="doctorLoading">{{ t("overview.doctor.loading") }}</template>
                <template v-else>
                  {{
                    t("projOverview.toolchainCardReady", {
                      coreReady: coreAvailableCount,
                      coreTotal: coreExecutionTools.length,
                      totalReady: totalAvailableCount,
                      total: doctorEntries.length,
                    })
                  }}
                </template>
              </p>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <div v-if="!doctorLoading" class="flex flex-wrap items-center gap-1.5">
              <span
                v-for="tool in coreExecutionTools"
                :key="tool.name"
                class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[11px] font-medium ring-1 ring-inset"
                :class="
                  tool.available
                    ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
                    : 'bg-amber-50 text-amber-800 ring-amber-600/20'
                "
              >
                <CheckCircle2 v-if="tool.available" class="h-3 w-3 text-emerald-600" />
                <XCircle v-else class="h-3 w-3 text-amber-600" />
                <span>{{ tool.name }}</span>
              </span>
            </div>

            <router-link
              to="/system/doctor"
              class="btn-secondary !px-3 !py-1.5 !text-xs"
              data-testid="overview-doctor-link"
            >
              <span>{{ t("projOverview.toolchainManageBtn") }}</span>
            </router-link>
          </div>
        </div>
      </section>
    </template>

    <!-- One-Click AI Acceptance Prompt Modal -->
    <AcceptancePromptModal
      :open="acceptancePromptOpen"
      :project-id="projectId"
      @close="acceptancePromptOpen = false"
    />
  </div>
</template>
