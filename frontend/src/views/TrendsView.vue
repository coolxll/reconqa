<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import * as echarts from "echarts";
import {
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  ExternalLink,
  GitCompare,
  Loader2,
  Minus,
  RefreshCw,
  TrendingUp,
} from "lucide-vue-next";
import { listProjects } from "@/api/projects";
import { getProjectTrends } from "@/api/reports";
import type { Project, ProjectTrendsResponse, TrendPoint } from "@/api/types";
import { useWorkspace } from "@/stores/workspace";
import EmptyState from "@/components/layout/EmptyState.vue";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import { useI18n } from "@/i18n";
import "@/i18n/locales/dashboards";

const route = useRoute();
const router = useRouter();
const { locale, t } = useI18n();
const { activeProjectId, ensureLoaded: ensureWorkspaceLoaded } = useWorkspace();

const routeProjectId = computed(() =>
  typeof route.params.projectId === "string" ? route.params.projectId : ""
);

const projects = ref<Project[]>([]);
const selectedProjectId = ref<string>("");
const loadingProjects = ref(true);
const loadingTrends = ref(false);
const error = ref<string | null>(null);
const trendsData = ref<ProjectTrendsResponse | null>(null);

const runAId = ref<string>("");
const runBId = ref<string>("");
const findingsChartMode = ref<"bar" | "area">("bar");

const coverageChartEl = ref<HTMLDivElement | null>(null);
const findingsChartEl = ref<HTMLDivElement | null>(null);
const perfChainsChartEl = ref<HTMLDivElement | null>(null);

let coverageChart: echarts.ECharts | null = null;
let findingsChart: echarts.ECharts | null = null;
let perfChainsChart: echarts.ECharts | null = null;
let resizeObserver: ResizeObserver | null = null;

const points = computed<TrendPoint[]>(() => trendsData.value?.trends ?? []);

const runA = computed<TrendPoint | null>(
  () => points.value.find((p) => p.run_id === runAId.value) ?? null
);
const runB = computed<TrendPoint | null>(
  () => points.value.find((p) => p.run_id === runBId.value) ?? null
);

function highCritCount(pt: TrendPoint | null): number {
  if (!pt) return 0;
  const sev = pt.findings_by_severity || {};
  return Number(sev.critical ?? 0) + Number(sev.high ?? 0);
}

function shortRunLabel(pt: TrendPoint, idx: number): string {
  const shortId = pt.run_id.slice(0, 8);
  if (pt.started_at) {
    const dt = new Date(pt.started_at);
    if (!Number.isNaN(dt.getTime())) {
      const mm = String(dt.getMonth() + 1).padStart(2, "0");
      const dd = String(dt.getDate()).padStart(2, "0");
      const hh = String(dt.getHours()).padStart(2, "0");
      const mi = String(dt.getMinutes()).padStart(2, "0");
      return `#${idx + 1} (${mm}-${dd} ${hh}:${mi})`;
    }
  }
  return `#${idx + 1} (${shortId})`;
}

interface DeltaCardItem {
  label: string;
  deltaText: string;
  baselineText: string;
  currentText: string;
  direction: "up" | "down" | "flat";
  sentiment: "positive" | "negative" | "neutral";
}

function buildDeltaCard(
  label: string,
  baseVal: number,
  currVal: number,
  unit: string,
  higherIsBetter: boolean,
  decimals = 0
): DeltaCardItem {
  const diff = Number((currVal - baseVal).toFixed(decimals));
  const direction: "up" | "down" | "flat" = diff > 0 ? "up" : diff < 0 ? "down" : "flat";
  const sign = diff > 0 ? "+" : "";
  const formattedDiff = decimals > 0 ? diff.toFixed(decimals) : String(diff);
  const deltaText = `${sign}${formattedDiff}${unit}`;

  let sentiment: "positive" | "negative" | "neutral" = "neutral";
  if (diff !== 0) {
    const improved = higherIsBetter ? diff > 0 : diff < 0;
    sentiment = improved ? "positive" : "negative";
  }

  return {
    label,
    deltaText,
    baselineText: `${decimals > 0 ? baseVal.toFixed(decimals) : baseVal}${unit}`,
    currentText: `${decimals > 0 ? currVal.toFixed(decimals) : currVal}${unit}`,
    direction,
    sentiment,
  };
}

const comparisonDeltas = computed<DeltaCardItem[]>(() => {
  if (!runA.value || !runB.value) return [];
  return [
    buildDeltaCard(
      t("trends.deltaCoverage"),
      Number(runA.value.coverage_pct ?? 0),
      Number(runB.value.coverage_pct ?? 0),
      "%",
      true,
      1
    ),
    buildDeltaCard(
      t("trends.deltaEndpoints"),
      Number(runA.value.total_endpoints ?? 0),
      Number(runB.value.total_endpoints ?? 0),
      t("trends.unitEndpoints"),
      true,
      0
    ),
    buildDeltaCard(
      t("trends.deltaHighCrit"),
      highCritCount(runA.value),
      highCritCount(runB.value),
      t("trends.unitFindings"),
      false,
      0
    ),
    buildDeltaCard(
      t("trends.deltaTotalFindings"),
      Number(runA.value.findings_total ?? 0),
      Number(runB.value.findings_total ?? 0),
      t("trends.unitFindings"),
      false,
      0
    ),
    buildDeltaCard(
      t("trends.deltaChains"),
      Number(runA.value.correlation_chains ?? 0),
      Number(runB.value.correlation_chains ?? 0),
      t("trends.unitChains"),
      true,
      0
    ),
    buildDeltaCard(
      t("trends.deltaSlow"),
      Number(runA.value.slow_endpoints_count ?? 0),
      Number(runB.value.slow_endpoints_count ?? 0),
      t("trends.unitSlow"),
      false,
      0
    ),
  ];
});

function renderCharts(): void {
  if (points.value.length === 0) return;

  if (!resizeObserver) {
    resizeObserver = new ResizeObserver(() => {
      coverageChart?.resize();
      findingsChart?.resize();
      perfChainsChart?.resize();
    });
  }

  const xLabels = points.value.map((pt, idx) => shortRunLabel(pt, idx));

  // Chart 1: API Coverage % & Endpoint Growth (Dual Axis)
  if (coverageChartEl.value) {
    if (!coverageChart) {
      coverageChart = echarts.init(coverageChartEl.value);
      resizeObserver.observe(coverageChartEl.value);
    }
    coverageChart.setOption(
      {
        tooltip: { trigger: "axis" },
        legend: {
          top: 0,
          textStyle: { color: "#475569", fontSize: 12 },
        },
        grid: { left: 16, right: 20, top: 40, bottom: 20, containLabel: true },
        xAxis: {
          type: "category",
          data: xLabels,
          axisLabel: { color: "#475569", fontSize: 11 },
          axisLine: { lineStyle: { color: "#e2e8f0" } },
        },
        yAxis: [
          {
            type: "value",
            name: t("trends.chart1YEndpoints"),
            nameTextStyle: { color: "#64748b", fontSize: 11 },
            splitLine: { lineStyle: { color: "#f1f5f9" } },
            axisLabel: { color: "#64748b", fontSize: 11 },
          },
          {
            type: "value",
            name: t("trends.chart1YCoverage"),
            min: 0,
            max: 100,
            nameTextStyle: { color: "#64748b", fontSize: 11 },
            splitLine: { show: false },
            axisLabel: { color: "#64748b", fontSize: 11, formatter: "{value}%" },
          },
        ],
        series: [
          {
            name: t("trends.seriesTotalEndpoints"),
            type: "bar",
            barMaxWidth: 26,
            data: points.value.map((p) => p.total_endpoints),
            itemStyle: { color: "#c7d2fe", borderRadius: [4, 4, 0, 0] },
          },
          {
            name: t("trends.seriesHarLive"),
            type: "bar",
            barMaxWidth: 26,
            data: points.value.map((p) => p.har_live_endpoints),
            itemStyle: { color: "#6366f1", borderRadius: [4, 4, 0, 0] },
          },
          {
            name: t("trends.seriesCoveragePct"),
            type: "line",
            yAxisIndex: 1,
            smooth: true,
            symbolSize: 8,
            data: points.value.map((p) => Number((p.coverage_pct ?? 0).toFixed(1))),
            lineStyle: { width: 3, color: "#10b981" },
            itemStyle: { color: "#10b981" },
          },
        ],
      },
      true
    );
  }

  // Chart 2: Security & Perf Findings by Severity (Stacked Bar / Area)
  if (findingsChartEl.value) {
    if (!findingsChart) {
      findingsChart = echarts.init(findingsChartEl.value);
      resizeObserver.observe(findingsChartEl.value);
    }
    const isArea = findingsChartMode.value === "area";
    const severities = [
      { key: "critical", name: t("sec.sevCritical"), color: "#e11d48" },
      { key: "high", name: t("sec.sevHigh"), color: "#f97316" },
      { key: "medium", name: t("sec.sevMedium"), color: "#f59e0b" },
      { key: "low", name: t("sec.sevLow"), color: "#0ea5e9" },
      { key: "info", name: t("sec.sevInfo"), color: "#94a3b8" },
    ];
    findingsChart.setOption(
      {
        tooltip: { trigger: "axis", axisPointer: { type: isArea ? "line" : "shadow" } },
        legend: {
          top: 0,
          textStyle: { color: "#475569", fontSize: 12 },
        },
        grid: { left: 16, right: 20, top: 40, bottom: 20, containLabel: true },
        xAxis: {
          type: "category",
          boundaryGap: !isArea,
          data: xLabels,
          axisLabel: { color: "#475569", fontSize: 11 },
          axisLine: { lineStyle: { color: "#e2e8f0" } },
        },
        yAxis: {
          type: "value",
          name: t("trends.chart2YFindings"),
          minInterval: 1,
          nameTextStyle: { color: "#64748b", fontSize: 11 },
          splitLine: { lineStyle: { color: "#f1f5f9" } },
          axisLabel: { color: "#64748b", fontSize: 11 },
        },
        series: severities.map((sev) => ({
          name: sev.name,
          type: isArea ? "line" : "bar",
          stack: "severity",
          smooth: isArea,
          areaStyle: isArea ? { opacity: 0.28 } : undefined,
          barMaxWidth: 32,
          data: points.value.map((p) => Number(p.findings_by_severity?.[sev.key] ?? 0)),
          itemStyle: { color: sev.color },
        })),
      },
      true
    );
  }

  // Chart 3: Slow Endpoints & Correlation Chains Trend
  if (perfChainsChartEl.value) {
    if (!perfChainsChart) {
      perfChainsChart = echarts.init(perfChainsChartEl.value);
      resizeObserver.observe(perfChainsChartEl.value);
    }
    perfChainsChart.setOption(
      {
        tooltip: { trigger: "axis" },
        legend: {
          top: 0,
          textStyle: { color: "#475569", fontSize: 12 },
        },
        grid: { left: 16, right: 20, top: 40, bottom: 20, containLabel: true },
        xAxis: {
          type: "category",
          data: xLabels,
          axisLabel: { color: "#475569", fontSize: 11 },
          axisLine: { lineStyle: { color: "#e2e8f0" } },
        },
        yAxis: {
          type: "value",
          minInterval: 1,
          splitLine: { lineStyle: { color: "#f1f5f9" } },
          axisLabel: { color: "#64748b", fontSize: 11 },
        },
        series: [
          {
            name: t("trends.seriesChains"),
            type: "bar",
            barMaxWidth: 28,
            data: points.value.map((p) => p.correlation_chains),
            itemStyle: { color: "#6366f1", borderRadius: [4, 4, 0, 0] },
          },
          {
            name: t("trends.seriesSlow"),
            type: "line",
            smooth: true,
            symbolSize: 8,
            data: points.value.map((p) => p.slow_endpoints_count),
            lineStyle: { width: 3, color: "#f59e0b" },
            itemStyle: { color: "#f59e0b" },
          },
        ],
      },
      true
    );
  }
}

function handleWindowResize(): void {
  coverageChart?.resize();
  findingsChart?.resize();
  perfChainsChart?.resize();
}

function disposeCharts(): void {
  resizeObserver?.disconnect();
  resizeObserver = null;
  coverageChart?.dispose();
  findingsChart?.dispose();
  perfChainsChart?.dispose();
  coverageChart = null;
  findingsChart = null;
  perfChainsChart = null;
}

let loadTrendsSeq = 0;

async function loadTrends(projectId: string): Promise<void> {
  const reqId = ++loadTrendsSeq;
  if (!projectId) {
    trendsData.value = null;
    disposeCharts();
    return;
  }
  loadingTrends.value = true;
  error.value = null;
  disposeCharts();
  try {
    const res = await getProjectTrends(projectId);
    if (reqId !== loadTrendsSeq) return;
    trendsData.value = res;
    if (res.trends.length >= 2) {
      runAId.value = res.trends[res.trends.length - 2].run_id;
      runBId.value = res.trends[res.trends.length - 1].run_id;
    } else if (res.trends.length === 1) {
      runAId.value = res.trends[0].run_id;
      runBId.value = res.trends[0].run_id;
    } else {
      runAId.value = "";
      runBId.value = "";
    }
  } catch (err) {
    if (reqId !== loadTrendsSeq) return;
    trendsData.value = null;
    error.value = err instanceof Error ? err.message : t("trends.errorLoadTrends");
  } finally {
    if (reqId === loadTrendsSeq) {
      loadingTrends.value = false;
    }
  }

  if (reqId === loadTrendsSeq && points.value.length > 0 && !loadingProjects.value) {
    await nextTick();
    renderCharts();
  }
}

async function initProjects(): Promise<void> {
  loadingProjects.value = true;
  error.value = null;
  let initialPid = "";
  try {
    await ensureWorkspaceLoaded();
    const list = await listProjects();
    projects.value = list;
    const preferredPid =
      routeProjectId.value ||
      (typeof route.query.projectId === "string" ? route.query.projectId : "") ||
      activeProjectId.value;
    if (preferredPid && list.some((p) => p.id === preferredPid)) {
      initialPid = preferredPid;
    } else if (list.length > 0) {
      initialPid = list[0].id;
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("trends.errorLoadProjects");
  } finally {
    loadingProjects.value = false;
  }

  if (initialPid) {
    if (selectedProjectId.value !== initialPid) {
      selectedProjectId.value = initialPid;
    } else if (points.value.length > 0 && !loadingTrends.value) {
      await nextTick();
      renderCharts();
    } else if (!loadingTrends.value) {
      await loadTrends(initialPid);
    }
  } else if (points.value.length > 0 && !loadingTrends.value) {
    await nextTick();
    renderCharts();
  }
}

watch(routeProjectId, (pid) => {
  if (pid && pid !== selectedProjectId.value) {
    selectedProjectId.value = pid;
  }
});

watch(selectedProjectId, (pid) => {
  if (pid) {
    void loadTrends(pid);
  }
});

watch(findingsChartMode, () => {
  renderCharts();
});

watch(locale, () => {
  renderCharts();
});

onMounted(() => {
  window.addEventListener("resize", handleWindowResize);
  void initProjects();
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", handleWindowResize);
  disposeCharts();
});
</script>

<template>
  <div class="space-y-6">
    <!-- Top Header & Project Selector -->
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div>
        <h1 class="text-xl font-semibold tracking-tight text-slate-900">
          {{ t("trends.title") }}
        </h1>
        <p class="mt-1 text-xs text-slate-500">
          {{ t("trends.subtitle") }}
        </p>
      </div>

      <div class="flex flex-wrap items-center gap-2.5">
        <select
          v-if="!routeProjectId"
          v-model="selectedProjectId"
          class="input-field w-60 text-sm"
          :disabled="loadingProjects || projects.length === 0"
          :aria-label="t('trends.selectProjectAria')"
        >
          <option v-if="projects.length === 0" value="">{{ t("trends.noProjectsOpt") }}</option>
          <option v-for="proj in projects" :key="proj.id" :value="proj.id">
            {{ proj.name }}
          </option>
        </select>

        <button
          type="button"
          class="btn-secondary text-xs"
          :disabled="!selectedProjectId || loadingTrends"
          @click="loadTrends(selectedProjectId)"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="loadingTrends ? 'animate-spin' : ''" />
          <span>{{ t("java.refresh") }}</span>
        </button>
      </div>
    </div>

    <!-- Error -->
    <div v-if="error" class="card border-rose-200 bg-rose-50/70 p-5 text-sm text-rose-800">
      {{ error }}
    </div>

    <!-- Loading -->
    <div
      v-else-if="loadingProjects || loadingTrends"
      class="card flex items-center justify-center gap-2.5 py-20 text-sm text-slate-500"
    >
      <Loader2 class="h-5 w-5 animate-spin text-indigo-600" />
      <span>{{ t("trends.loading") }}</span>
    </div>

    <!-- No Projects -->
    <EmptyState
      v-else-if="projects.length === 0"
      :title="t('trends.noProjectsTitle')"
      :description="t('trends.noProjectsDesc')"
    >
      <template #icon>
        <TrendingUp class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <button type="button" class="btn-primary text-xs" @click="router.push('/projects')">
          {{ t("trends.goProjects") }}
        </button>
      </template>
    </EmptyState>

    <!-- No Runs for Project -->
    <EmptyState
      v-else-if="points.length === 0"
      :title="t('trends.noRunsTitle')"
      :description="t('trends.noRunsDesc')"
    >
      <template #icon>
        <TrendingUp class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <button
          type="button"
          class="btn-primary text-xs"
          @click="router.push(selectedProjectId ? `/p/${selectedProjectId}/runs?new=1` : '/runs')"
        >
          {{ t("trends.launchRun") }}
        </button>
      </template>
    </EmptyState>

    <template v-else>
      <!-- Run-to-Run Diff Comparison -->
      <div class="card overflow-hidden">
        <div class="card-header flex-wrap">
          <div class="flex items-center gap-2.5">
            <GitCompare class="h-4 w-4 text-indigo-600" />
            <div>
              <h2 class="text-sm font-semibold text-slate-900">{{ t("trends.diffTitle") }}</h2>
              <p class="text-xs text-slate-500">
                {{ t("trends.diffDesc") }}
              </p>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <div class="flex items-center gap-1.5">
              <span class="text-xs font-medium text-slate-500">{{ t("trends.runALabel") }}</span>
              <select v-model="runAId" class="input-field w-48 py-1 text-xs">
                <option v-for="(pt, idx) in points" :key="pt.run_id" :value="pt.run_id">
                  {{ shortRunLabel(pt, idx) }}
                </option>
              </select>
            </div>
            <ArrowRight class="h-4 w-4 text-slate-400" />
            <div class="flex items-center gap-1.5">
              <span class="text-xs font-medium text-slate-500">{{ t("trends.runBLabel") }}</span>
              <select v-model="runBId" class="input-field w-48 py-1 text-xs">
                <option v-for="(pt, idx) in points" :key="pt.run_id" :value="pt.run_id">
                  {{ shortRunLabel(pt, idx) }}
                </option>
              </select>
            </div>
          </div>
        </div>

        <div class="p-5">
          <div
            v-if="points.length === 1"
            class="mb-4 rounded-lg border border-indigo-100 bg-indigo-50/60 px-3.5 py-2 text-xs text-indigo-700"
          >
            {{ t("trends.singleRunHint") }}
          </div>

          <div class="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-6">
            <div
              v-for="card in comparisonDeltas"
              :key="card.label"
              class="rounded-xl border p-3.5 transition"
              :class="
                card.sentiment === 'positive'
                  ? 'border-emerald-200 bg-emerald-50/40'
                  : card.sentiment === 'negative'
                    ? 'border-rose-200 bg-rose-50/40'
                    : 'border-slate-200 bg-slate-50/50'
              "
            >
              <p class="truncate text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                {{ card.label }}
              </p>
              <div class="mt-1.5 flex items-center gap-1">
                <ArrowUpRight
                  v-if="card.direction === 'up'"
                  class="h-4 w-4 shrink-0"
                  :class="card.sentiment === 'positive' ? 'text-emerald-600' : 'text-rose-600'"
                />
                <ArrowDownRight
                  v-else-if="card.direction === 'down'"
                  class="h-4 w-4 shrink-0"
                  :class="card.sentiment === 'positive' ? 'text-emerald-600' : 'text-rose-600'"
                />
                <Minus v-else class="h-4 w-4 shrink-0 text-slate-400" />
                <span
                  class="truncate text-base font-bold tabular-nums"
                  :class="
                    card.sentiment === 'positive'
                      ? 'text-emerald-700'
                      : card.sentiment === 'negative'
                        ? 'text-rose-700'
                        : 'text-slate-700'
                  "
                >
                  {{ card.deltaText }}
                </span>
              </div>
              <p class="mt-1 truncate font-mono text-[11px] text-slate-500">
                {{ card.baselineText }} &rarr; {{ card.currentText }}
              </p>
            </div>
          </div>

          <!-- Quick Jump Links -->
          <div v-if="runA && runB" class="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3 text-xs">
            <div class="flex flex-wrap items-center gap-3 text-slate-600">
              <span class="inline-flex items-center gap-1.5">
                {{ t("trends.baselineTag") }}
                <code class="font-mono text-slate-800">{{ runA.run_id.slice(0, 12) }}</code>
                <StatusBadge :status="runA.status" />
              </span>
              <span class="inline-flex items-center gap-1.5">
                {{ t("trends.currentTag") }}
                <code class="font-mono text-slate-800">{{ runB.run_id.slice(0, 12) }}</code>
                <StatusBadge :status="runB.status" />
              </span>
            </div>
            <div class="flex items-center gap-3">
              <router-link
                :to="selectedProjectId ? `/p/${selectedProjectId}/runs/${runA.run_id}` : `/runs/${runA.run_id}`"
                class="inline-flex items-center gap-1 font-medium text-indigo-600 hover:text-indigo-800"
              >
                <span>{{ t("trends.openRunA") }}</span>
                <ExternalLink class="h-3 w-3" />
              </router-link>
              <router-link
                :to="selectedProjectId ? `/p/${selectedProjectId}/runs/${runB.run_id}` : `/runs/${runB.run_id}`"
                class="inline-flex items-center gap-1 font-medium text-indigo-600 hover:text-indigo-800"
              >
                <span>{{ t("trends.openRunB") }}</span>
                <ExternalLink class="h-3 w-3" />
              </router-link>
            </div>
          </div>
        </div>
      </div>

      <!-- Chart 1: API Coverage % & Endpoint Growth -->
      <div class="card overflow-hidden">
        <div class="card-header">
          <div>
            <h2 class="text-sm font-semibold text-slate-900">
              {{ t("trends.chart1Title") }}
            </h2>
            <p class="text-xs text-slate-500">
              {{ t("trends.chart1Desc", { count: points.length }) }}
            </p>
          </div>
        </div>
        <div class="p-4">
          <div ref="coverageChartEl" class="h-80 w-full" />
        </div>
      </div>

      <!-- Charts 2 & 3 Side-by-Side -->
      <div class="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <!-- Chart 2: Findings by Severity -->
        <div class="card overflow-hidden">
          <div class="card-header">
            <div>
              <h2 class="text-sm font-semibold text-slate-900">
                {{ t("trends.chart2Title") }}
              </h2>
              <p class="text-xs text-slate-500">
                {{ t("trends.chart2Desc") }}
              </p>
            </div>
            <div class="flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-xs">
              <button
                type="button"
                class="rounded-md px-2.5 py-1 transition"
                :class="
                  findingsChartMode === 'bar'
                    ? 'bg-white font-medium text-indigo-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900'
                "
                @click="findingsChartMode = 'bar'"
              >
                {{ t("trends.modeBar") }}
              </button>
              <button
                type="button"
                class="rounded-md px-2.5 py-1 transition"
                :class="
                  findingsChartMode === 'area'
                    ? 'bg-white font-medium text-indigo-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900'
                "
                @click="findingsChartMode = 'area'"
              >
                {{ t("trends.modeArea") }}
              </button>
            </div>
          </div>
          <div class="p-4">
            <div ref="findingsChartEl" class="h-72 w-full" />
          </div>
        </div>

        <!-- Chart 3: Slow Endpoints & Correlation Chains -->
        <div class="card overflow-hidden">
          <div class="card-header">
            <div>
              <h2 class="text-sm font-semibold text-slate-900">
                {{ t("trends.chart3Title") }}
              </h2>
              <p class="text-xs text-slate-500">
                {{ t("trends.chart3Desc") }}
              </p>
            </div>
          </div>
          <div class="p-4">
            <div ref="perfChainsChartEl" class="h-72 w-full" />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
