<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts";
import {
  Activity,
  AlertTriangle,
  Check,
  Copy,
  Download,
  Gauge,
  Loader2,
  Play,
  RefreshCw,
  Terminal,
  Zap,
} from "lucide-vue-next";
import { artifactDownloadUrl } from "@/api/client";
import { getRunPerformance } from "@/api/reports";
import { executeRunModules, getRun } from "@/api/runs";
import type { PerfEndpointMetric, PerformanceReport } from "@/api/types";
import EmptyState from "@/components/layout/EmptyState.vue";
import MetricCard from "@/components/layout/MetricCard.vue";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import { useI18n } from "@/i18n";
import "@/i18n/locales/dashboards";

const props = defineProps<{
  runId: string;
  projectId?: string;
}>();

const emit = defineEmits<{
  (e: "executed"): void;
}>();

const { locale, t } = useI18n();

const loading = ref(true);
const executingMode = ref<null | "har" | "k6">(null);
const error = ref<string | null>(null);
const execError = ref<string | null>(null);
const report = ref<PerformanceReport | null>(null);
const k6TargetUrl = ref("");
const copiedCmd = ref(false);
let isUnmounted = false;

const slowestChartEl = ref<HTMLDivElement | null>(null);
const payloadChartEl = ref<HTMLDivElement | null>(null);
const cacheChartEl = ref<HTMLDivElement | null>(null);

let slowestChart: echarts.ECharts | null = null;
let payloadChart: echarts.ECharts | null = null;
let cacheChart: echarts.ECharts | null = null;
let resizeObserver: ResizeObserver | null = null;

const k6ScriptUrl = computed(() => artifactDownloadUrl(props.runId, "k6_script"));
const k6BundleUrl = computed(() => artifactDownloadUrl(props.runId, "k6"));

const k6Command =
  "HTTP_PROXY=http://127.0.0.1:8888 HTTPS_PROXY=http://127.0.0.1:8888 \\\n  k6 run -e BASE_URL=https://api.example.com -e NO_CONN_REUSE=false scenario_ramp.js";

const profiler = computed<Record<string, unknown> | null>(() => {
  return (report.value?.har_profiler as Record<string, unknown> | null) ?? null;
});

const summaryObj = computed<Record<string, unknown>>(() => {
  const p = profiler.value;
  if (!p) return {};
  return (p.summary && typeof p.summary === "object"
    ? (p.summary as Record<string, unknown>)
    : p) as Record<string, unknown>;
});

const slowestTtfb = computed<PerfEndpointMetric[]>(() => {
  const raw = profiler.value?.slowest_ttfb;
  return Array.isArray(raw) ? (raw as PerfEndpointMetric[]) : [];
});

const largestPayloads = computed<PerfEndpointMetric[]>(() => {
  const raw = profiler.value?.largest_bodies ?? profiler.value?.largest_responses;
  return Array.isArray(raw) ? (raw as PerfEndpointMetric[]) : [];
});

const uncompressedResponses = computed<Array<Record<string, unknown>>>(() => {
  const raw = profiler.value?.uncompressed_responses;
  return Array.isArray(raw) ? (raw as Array<Record<string, unknown>>) : [];
});

const duplicateRequests = computed<Array<Record<string, unknown>>>(() => {
  const raw = profiler.value?.duplicate_requests;
  return Array.isArray(raw) ? (raw as Array<Record<string, unknown>>) : [];
});

const cacheStats = computed(() => {
  const cs = (profiler.value?.cache_stats ?? {}) as Record<string, unknown>;
  const hit = Number(cs.hit ?? 0);
  const miss = Number(cs.miss ?? 0);
  const bypass = Number(cs.bypass_or_dynamic ?? cs.bypass ?? 0) + Number(cs.dynamic ?? 0);
  const cacheControlOnly = Number(cs.cache_control_only ?? 0);
  const noCacheHeader = Number(cs.no_cache_header ?? 0);
  const rawRate = cs.cdn_hit_rate;
  let hitRatePct: number | null = null;
  if (typeof rawRate === "number") {
    hitRatePct = Math.round(rawRate * 1000) / 10;
  } else if (typeof cs.hit_rate_pct === "number") {
    hitRatePct = Math.round(cs.hit_rate_pct * 10) / 10;
  } else if (hit + miss > 0) {
    hitRatePct = Math.round((hit / (hit + miss)) * 1000) / 10;
  }
  return { hit, miss, bypass, cacheControlOnly, noCacheHeader, hitRatePct };
});

const kpis = computed(() => {
  const s = summaryObj.value;
  const totalRequests = Number(s.total_requests ?? profiler.value?.total_requests ?? 0);
  const totalBytes = Number(s.total_bytes ?? profiler.value?.total_bytes ?? 0);
  const totalMb = `${(totalBytes / (1024 * 1024)).toFixed(2)} MB`;

  const topTtfb = slowestTtfb.value.reduce((max, r) => {
    const v = Number(r.ttfb_ms ?? r.wait_ms ?? 0);
    return v > max ? v : max;
  }, 0);
  const p95Ttfb = Number(s.p95_ttfb_ms ?? 0);

  return {
    totalRequests,
    totalMb,
    slowestTtfbLabel: `${Math.round(topTtfb)} ms`,
    p95Subtitle:
      p95Ttfb > 0 ? t("perf.kpiP95Sub", { ms: Math.round(p95Ttfb) }) : t("perf.kpiHighestWaitSub"),
    uncompressedCount: Number(s.uncompressed_count ?? uncompressedResponses.value.length),
    duplicateCount: Number(s.duplicate_group_count ?? duplicateRequests.value.length),
    cacheHitLabel:
      cacheStats.value.hitRatePct !== null ? `${cacheStats.value.hitRatePct}%` : "N/A",
  };
});

const k6Normalized = computed(() => {
  const k6 = report.value?.k6_analysis as Record<string, unknown> | null | undefined;
  if (!k6) return null;

  const metrics =
    k6.metrics && typeof k6.metrics === "object"
      ? (k6.metrics as Record<string, unknown>)
      : k6;
  const latency =
    metrics.latency_ms && typeof metrics.latency_ms === "object"
      ? (metrics.latency_ms as Record<string, unknown>)
      : {};

  const p95 = Number(latency.p95 ?? k6.p95_ms ?? 0);
  const p90 = Number(latency.p90 ?? k6.p90_ms ?? 0);
  const avg = Number(latency.avg ?? k6.p50_ms ?? 0);
  const rps = Number(metrics.rps ?? k6.rps ?? 0);
  const reqCount = Number(metrics.http_reqs_count ?? k6.total_requests ?? 0);
  const rate429 = Number(metrics.http_429_rate ?? k6.http_429_rate ?? 0);
  const rate5xx = Number(metrics.http_5xx_rate ?? k6.http_5xx_rate ?? 0);
  const count429 = Number(metrics.http_429_count ?? 0);
  const count5xx = Number(metrics.http_5xx_count ?? 0);

  const diagObj =
    k6.diagnosis && typeof k6.diagnosis === "object"
      ? (k6.diagnosis as Record<string, unknown>)
      : null;
  const status = String(k6.status ?? (reqCount > 0 ? "completed" : "skipped"));
  const inflectionDetected = diagObj
    ? Boolean(diagObj.inflection_detected)
    : rate429 >= 0.05 || rate5xx >= 0.02 || p95 >= 2000;
  const notes: string[] = diagObj && Array.isArray(diagObj.notes)
    ? diagObj.notes.map(String)
    : typeof k6.diagnosis === "string"
      ? [k6.diagnosis]
      : typeof k6.reason === "string"
        ? [k6.reason]
        : [];
  const breachedThresholds: string[] =
    diagObj && Array.isArray(diagObj.breached_thresholds)
      ? diagObj.breached_thresholds.map(String)
      : [];

  return {
    status,
    hasMetrics: reqCount > 0 || p95 > 0 || rps > 0,
    p95,
    p90,
    avg,
    rps,
    reqCount,
    rate429,
    rate5xx,
    count429,
    count5xx,
    inflectionDetected,
    notes,
    breachedThresholds,
  };
});

function shortEndpointLabel(item: PerfEndpointMetric): string {
  const method = String(item.method ?? "GET").toUpperCase();
  const rawPath = String(item.path ?? item.url ?? "/");
  const trimmed = rawPath.length > 36 ? `${rawPath.slice(0, 33)}...` : rawPath;
  return `${method} ${trimmed}`;
}

function formatKb(bytes: unknown): string {
  const num = Number(bytes ?? 0);
  if (num >= 1024 * 1024) return `${(num / (1024 * 1024)).toFixed(2)} MB`;
  return `${(num / 1024).toFixed(1)} KB`;
}

async function copyK6Command(): Promise<void> {
  try {
    await navigator.clipboard.writeText(k6Command);
    copiedCmd.value = true;
    window.setTimeout(() => {
      copiedCmd.value = false;
    }, 1800);
  } catch {
    // Ignore clipboard error
  }
}

function renderCharts(): void {
  if (!profiler.value) return;

  if (!resizeObserver) {
    resizeObserver = new ResizeObserver(() => {
      slowestChart?.resize();
      payloadChart?.resize();
      cacheChart?.resize();
    });
  }

  // 1. Slowest Endpoints (TTFB vs Total ms)
  if (slowestChartEl.value && slowestTtfb.value.length > 0) {
    if (!slowestChart) {
      slowestChart = echarts.init(slowestChartEl.value);
      resizeObserver.observe(slowestChartEl.value);
    }
    const items = [...slowestTtfb.value].slice(0, 8).reverse();
    slowestChart.setOption(
      {
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "shadow" },
        },
        legend: {
          top: 0,
          right: 0,
          textStyle: { color: "#475569", fontSize: 11 },
        },
        grid: { left: 12, right: 20, top: 30, bottom: 8, containLabel: true },
        xAxis: {
          type: "value",
          name: "ms",
          nameTextStyle: { color: "#64748b", fontSize: 11 },
          splitLine: { lineStyle: { color: "#f1f5f9" } },
          axisLabel: { color: "#64748b", fontSize: 11 },
        },
        yAxis: {
          type: "category",
          data: items.map(shortEndpointLabel),
          axisLabel: { color: "#334155", fontSize: 11, fontFamily: "monospace" },
          axisLine: { lineStyle: { color: "#e2e8f0" } },
        },
        series: [
          {
            name: t("perf.seriesTtfb"),
            type: "bar",
            data: items.map((r) => Number(r.ttfb_ms ?? r.wait_ms ?? 0)),
            itemStyle: { color: "#6366f1", borderRadius: [0, 4, 4, 0] },
            barMaxWidth: 14,
          },
          {
            name: t("perf.seriesTotal"),
            type: "bar",
            data: items.map((r) => Number(r.total_ms ?? 0)),
            itemStyle: { color: "#cbd5e1", borderRadius: [0, 4, 4, 0] },
            barMaxWidth: 14,
          },
        ],
      },
      true
    );
  }

  // 2. Largest Payloads Top N (KB)
  if (payloadChartEl.value && largestPayloads.value.length > 0) {
    if (!payloadChart) {
      payloadChart = echarts.init(payloadChartEl.value);
      resizeObserver.observe(payloadChartEl.value);
    }
    const items = [...largestPayloads.value].slice(0, 8).reverse();
    payloadChart.setOption(
      {
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "shadow" },
          valueFormatter: (val: unknown) => `${val} KB`,
        },
        grid: { left: 12, right: 24, top: 18, bottom: 8, containLabel: true },
        xAxis: {
          type: "value",
          name: "KB",
          nameTextStyle: { color: "#64748b", fontSize: 11 },
          splitLine: { lineStyle: { color: "#f1f5f9" } },
          axisLabel: { color: "#64748b", fontSize: 11 },
        },
        yAxis: {
          type: "category",
          data: items.map(shortEndpointLabel),
          axisLabel: { color: "#334155", fontSize: 11, fontFamily: "monospace" },
          axisLine: { lineStyle: { color: "#e2e8f0" } },
        },
        series: [
          {
            name: t("perf.seriesPayload"),
            type: "bar",
            data: items.map((r) => Math.round((Number(r.size_bytes ?? 0) / 1024) * 10) / 10),
            itemStyle: {
              color: (params: { dataIndex: number }) => {
                const row = items[params.dataIndex];
                const bytes = Number(row?.size_bytes ?? 0);
                return bytes >= 100_000 ? "#f59e0b" : "#0ea5e9";
              },
              borderRadius: [0, 4, 4, 0],
            },
            barMaxWidth: 18,
          },
        ],
      },
      true
    );
  }

  // 3. Cache / CDN Donut Chart
  if (cacheChartEl.value) {
    if (!cacheChart) {
      cacheChart = echarts.init(cacheChartEl.value);
      resizeObserver.observe(cacheChartEl.value);
    }
    const cs = cacheStats.value;
    const pieData = [
      { value: cs.hit, name: t("perf.cacheHit"), itemStyle: { color: "#10b981" } },
      { value: cs.miss, name: t("perf.cacheMiss"), itemStyle: { color: "#f43f5e" } },
      { value: cs.bypass, name: t("perf.cacheBypass"), itemStyle: { color: "#f59e0b" } },
      {
        value: cs.cacheControlOnly,
        name: t("perf.cacheControlOnly"),
        itemStyle: { color: "#6366f1" },
      },
      { value: cs.noCacheHeader, name: t("perf.cacheNoHeader"), itemStyle: { color: "#94a3b8" } },
    ].filter((d) => d.value > 0);

    cacheChart.setOption(
      {
        tooltip: { trigger: "item", formatter: "{b}: <b>{c}</b> ({d}%)" },
        legend: {
          bottom: 0,
          icon: "circle",
          itemWidth: 9,
          itemHeight: 9,
          textStyle: { color: "#475569", fontSize: 11 },
        },
        title: {
          text: kpis.value.cacheHitLabel,
          subtext: t("perf.cacheCenterSub"),
          left: "center",
          top: "36%",
          textStyle: { fontSize: 20, fontWeight: 700, color: "#0f172a" },
          subtextStyle: { fontSize: 11, color: "#64748b" },
        },
        series: [
          {
            name: t("perf.seriesCache"),
            type: "pie",
            radius: ["54%", "74%"],
            center: ["50%", "44%"],
            itemStyle: { borderRadius: 5, borderColor: "#ffffff", borderWidth: 2 },
            label: { show: false },
            data:
              pieData.length > 0
                ? pieData
                : [{ value: 1, name: t("perf.cacheNoData"), itemStyle: { color: "#e2e8f0" } }],
          },
        ],
      },
      true
    );
  }
}

function handleWindowResize(): void {
  slowestChart?.resize();
  payloadChart?.resize();
  cacheChart?.resize();
}

function disposeCharts(): void {
  resizeObserver?.disconnect();
  resizeObserver = null;
  slowestChart?.dispose();
  payloadChart?.dispose();
  cacheChart?.dispose();
  slowestChart = null;
  payloadChart = null;
  cacheChart = null;
}

async function loadPerformance(): Promise<void> {
  if (!props.runId) return;
  loading.value = true;
  error.value = null;
  disposeCharts();
  try {
    report.value = await getRunPerformance(props.runId);
  } catch (err) {
    report.value = null;
    const msg = err instanceof Error ? err.message : "";
    if (!msg.includes("404") && !msg.toLowerCase().includes("not found")) {
      error.value = msg || t("perf.errorFallback");
    }
  } finally {
    loading.value = false;
  }

  if (report.value && profiler.value) {
    await nextTick();
    renderCharts();
  }
}

async function handleRunPerformance(withK6: boolean): Promise<void> {
  if (!props.runId || executingMode.value) return;
  executingMode.value = withK6 ? "k6" : "har";
  execError.value = null;
  try {
    const config: Record<string, unknown> = { run_k6: withK6 };
    if (withK6 && k6TargetUrl.value.trim()) {
      config.target_url = k6TargetUrl.value.trim();
    }
    await executeRunModules(props.runId, { modules: ["perf"], config });
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
      execError.value = t("perf.execFailed");
    }
    await loadPerformance();
    emit("executed");
  } catch (err) {
    execError.value = err instanceof Error ? err.message : t("perf.execFailed");
  } finally {
    executingMode.value = null;
  }
}

watch(
  () => props.runId,
  () => {
    execError.value = null;
    void loadPerformance();
  }
);

watch(locale, () => {
  renderCharts();
});

onMounted(() => {
  isUnmounted = false;
  window.addEventListener("resize", handleWindowResize);
  void loadPerformance();
});

onBeforeUnmount(() => {
  isUnmounted = true;
  window.removeEventListener("resize", handleWindowResize);
  disposeCharts();
});
</script>

<template>
  <div class="space-y-6">
    <!-- Contextual On-Demand Execution Bar -->
    <div class="card p-4">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div class="min-w-0">
          <h3 class="text-sm font-semibold text-slate-900">{{ t("perf.actionTitle") }}</h3>
          <p class="mt-0.5 text-xs text-slate-500">{{ t("perf.actionDesc") }}</p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="perf-run-har-btn"
            class="btn-primary py-1.5 text-xs"
            :disabled="executingMode !== null"
            @click="handleRunPerformance(false)"
          >
            <Loader2 v-if="executingMode === 'har'" class="h-3.5 w-3.5 animate-spin" />
            <Play v-else class="h-3.5 w-3.5" />
            <span>
              {{
                executingMode === "har"
                  ? t("perf.runningHarBtn")
                  : profiler
                    ? t("perf.rerunHarBtn")
                    : t("perf.runHarBtn")
              }}
            </span>
          </button>

          <div class="flex items-center gap-1.5">
            <input
              v-model="k6TargetUrl"
              type="text"
              data-testid="perf-k6-url-input"
              class="input-field w-56 py-1.5 font-mono text-xs"
              :placeholder="t('perf.k6TargetPlaceholder')"
              :disabled="executingMode !== null"
            />
            <button
              type="button"
              data-testid="perf-run-k6-btn"
              class="btn-secondary shrink-0 py-1.5 text-xs"
              :disabled="executingMode !== null"
              @click="handleRunPerformance(true)"
            >
              <Loader2 v-if="executingMode === 'k6'" class="h-3.5 w-3.5 animate-spin" />
              <Zap v-else class="h-3.5 w-3.5 text-amber-500" />
              <span>{{ executingMode === "k6" ? t("perf.runningK6Btn") : t("perf.runK6Btn") }}</span>
            </button>
          </div>
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
      <span>{{ t("perf.loading") }}</span>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="card border-rose-200 bg-rose-50/70 p-6">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h3 class="text-sm font-semibold text-rose-800">{{ t("perf.errorTitle") }}</h3>
          <p class="mt-1 text-xs text-rose-700">{{ error }}</p>
        </div>
        <button type="button" class="btn-secondary text-xs" @click="loadPerformance">
          <RefreshCw class="h-3.5 w-3.5" />
          <span>{{ t("java.retry") }}</span>
        </button>
      </div>
    </div>

    <!-- Empty -->
    <EmptyState
      v-else-if="!report || (!profiler && !k6Normalized)"
      :title="t('perf.emptyTitle')"
      :description="t('perf.emptyDesc')"
    >
      <template #icon>
        <Gauge class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <button type="button" class="btn-secondary text-xs" @click="loadPerformance">
          <RefreshCw class="h-3.5 w-3.5" />
          <span>{{ t("java.refresh") }}</span>
        </button>
      </template>
    </EmptyState>

    <template v-else>
      <!-- 1. KPI Cards -->
      <div v-if="profiler" class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-6">
        <MetricCard
          :label="t('perf.kpiTotalReq')"
          :value="kpis.totalRequests"
          :subtitle="t('perf.kpiTotalReqSub')"
          tone="indigo"
        />
        <MetricCard
          :label="t('perf.kpiTotalPayload')"
          :value="kpis.totalMb"
          :subtitle="t('perf.kpiTotalPayloadSub')"
          tone="neutral"
        />
        <MetricCard
          :label="t('perf.kpiSlowestTtfb')"
          :value="kpis.slowestTtfbLabel"
          :subtitle="kpis.p95Subtitle"
          :tone="kpis.slowestTtfbLabel.startsWith('0') ? 'emerald' : 'amber'"
        />
        <MetricCard
          :label="t('perf.kpiUncompressed')"
          :value="kpis.uncompressedCount"
          :subtitle="t('perf.kpiUncompressedSub')"
          :tone="kpis.uncompressedCount > 0 ? 'rose' : 'emerald'"
        />
        <MetricCard
          :label="t('perf.kpiDuplicates')"
          :value="kpis.duplicateCount"
          :subtitle="t('perf.kpiDuplicatesSub')"
          :tone="kpis.duplicateCount > 0 ? 'amber' : 'emerald'"
        />
        <MetricCard
          :label="t('perf.kpiCacheHit')"
          :value="kpis.cacheHitLabel"
          :subtitle="t('perf.kpiCacheHitSub')"
          tone="emerald"
        />
      </div>

      <!-- 2. Slowest Top N & Largest Payloads Horizontal Bar Charts -->
      <div v-if="profiler" class="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <div class="card overflow-hidden">
          <div class="card-header">
            <div>
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("perf.slowestTitle") }}
              </h3>
              <p class="text-xs text-slate-500">
                {{ t("perf.slowestDesc") }}
              </p>
            </div>
            <Activity class="h-4 w-4 text-indigo-500" />
          </div>
          <div class="p-4">
            <div v-if="slowestTtfb.length === 0" class="py-12 text-center text-xs text-slate-400">
              {{ t("perf.noLatency") }}
            </div>
            <div v-else ref="slowestChartEl" class="h-72 w-full" />
          </div>
        </div>

        <div class="card overflow-hidden">
          <div class="card-header">
            <div>
              <h3 class="text-sm font-semibold text-slate-900">{{ t("perf.payloadTitle") }}</h3>
              <p class="text-xs text-slate-500">
                {{ t("perf.payloadDesc") }}
              </p>
            </div>
            <Zap class="h-4 w-4 text-amber-500" />
          </div>
          <div class="p-4">
            <div v-if="largestPayloads.length === 0" class="py-12 text-center text-xs text-slate-400">
              {{ t("perf.noPayload") }}
            </div>
            <div v-else ref="payloadChartEl" class="h-72 w-full" />
          </div>
        </div>
      </div>

      <!-- 3. Cache/CDN Donut + Uncompressed & Duplicate Tables -->
      <div v-if="profiler" class="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <div class="card lg:col-span-4">
          <div class="card-header">
            <div>
              <h3 class="text-sm font-semibold text-slate-900">{{ t("perf.cacheTitle") }}</h3>
              <p class="text-xs text-slate-500">{{ t("perf.cacheDesc") }}</p>
            </div>
          </div>
          <div class="p-4">
            <div ref="cacheChartEl" class="h-64 w-full" />
          </div>
        </div>

        <div class="space-y-5 lg:col-span-8">
          <!-- Uncompressed Responses -->
          <div class="card overflow-hidden">
            <div class="card-header">
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("perf.uncompressedTitle") }}
              </h3>
              <span class="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600">
                {{ uncompressedResponses.length }}
              </span>
            </div>
            <div
              v-if="uncompressedResponses.length === 0"
              class="px-5 py-6 text-center text-xs text-slate-500"
            >
              {{ t("perf.uncompressedEmpty") }}
            </div>
            <div v-else class="max-h-48 overflow-auto">
              <table class="min-w-full divide-y divide-slate-200 text-xs">
                <thead class="table-head">
                  <tr>
                    <th class="px-4 py-2">{{ t("perf.colEndpoint") }}</th>
                    <th class="px-3 py-2">{{ t("perf.colMime") }}</th>
                    <th class="px-3 py-2">{{ t("perf.colEncoding") }}</th>
                    <th class="px-4 py-2 text-right">{{ t("perf.colSize") }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-slate-100 bg-white">
                  <tr v-for="(u, idx) in uncompressedResponses" :key="idx" class="hover:bg-slate-50/70">
                    <td class="max-w-xs truncate px-4 py-2 font-mono text-slate-800">
                      {{ u.endpoint || u.url }}
                    </td>
                    <td class="px-3 py-2 font-mono text-slate-500">
                      {{ u.mime_type || u.content_type || "—" }}
                    </td>
                    <td class="px-3 py-2">
                      <span class="rounded bg-rose-50 px-1.5 py-0.5 font-mono text-[11px] text-rose-700">
                        {{ u.content_encoding || u.encoding || "none" }}
                      </span>
                    </td>
                    <td class="px-4 py-2 text-right font-mono font-semibold text-amber-700">
                      {{ formatKb(u.size_bytes) }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Duplicate Redundant Requests -->
          <div class="card overflow-hidden">
            <div class="card-header">
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("perf.duplicatesTitle") }}
              </h3>
              <span class="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600">
                {{ duplicateRequests.length }}
              </span>
            </div>
            <div
              v-if="duplicateRequests.length === 0"
              class="px-5 py-6 text-center text-xs text-slate-500"
            >
              {{ t("perf.duplicatesEmpty") }}
            </div>
            <div v-else class="max-h-48 overflow-auto">
              <table class="min-w-full divide-y divide-slate-200 text-xs">
                <thead class="table-head">
                  <tr>
                    <th class="px-4 py-2">{{ t("perf.colCount") }}</th>
                    <th class="px-3 py-2">{{ t("perf.colScenario") }}</th>
                    <th class="px-3 py-2">{{ t("perf.colMethod") }}</th>
                    <th class="px-4 py-2">{{ t("perf.colUrl") }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-slate-100 bg-white">
                  <tr v-for="(d, idx) in duplicateRequests" :key="idx" class="hover:bg-slate-50/70">
                    <td class="px-4 py-2">
                      <span class="rounded-full bg-amber-50 px-2 py-0.5 font-mono font-semibold text-amber-800 ring-1 ring-amber-200">
                        {{ d.count }}x
                      </span>
                    </td>
                    <td class="px-3 py-2 font-mono text-slate-600">{{ d.scenario || "default" }}</td>
                    <td class="px-3 py-2 font-mono font-semibold text-slate-700">{{ d.method || "GET" }}</td>
                    <td class="max-w-md truncate px-4 py-2 font-mono text-slate-700" :title="String(d.url || '')">
                      {{ d.url }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <!-- 4. k6 Load Test Panel -->
      <div class="card overflow-hidden">
        <div class="card-header flex-wrap">
          <div class="flex items-center gap-2.5">
            <Terminal class="h-4 w-4 text-indigo-600" />
            <div>
              <h3 class="text-sm font-semibold text-slate-900">{{ t("perf.k6Title") }}</h3>
              <p class="text-xs text-slate-500">
                {{ t("perf.k6Desc") }}
              </p>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <span
              v-if="k6Normalized"
              class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset"
              :class="
                k6Normalized.status === 'skipped'
                  ? 'bg-slate-100 text-slate-700 ring-slate-300'
                  : k6Normalized.inflectionDetected
                    ? 'bg-rose-50 text-rose-700 ring-rose-600/20'
                    : 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
              "
            >
              <AlertTriangle v-if="k6Normalized.inflectionDetected" class="h-3.5 w-3.5" />
              <span>
                {{
                  k6Normalized.status === "skipped"
                    ? t("perf.k6Skipped")
                    : k6Normalized.inflectionDetected
                      ? t("perf.k6Inflection")
                      : t("perf.k6Healthy")
                }}
              </span>
            </span>

            <a
              v-if="report.k6_script_available"
              :href="k6ScriptUrl"
              download="scenario_ramp.js"
              class="btn-primary text-xs"
            >
              <Download class="h-3.5 w-3.5" />
              <span>{{ t("perf.downloadScript") }}</span>
            </a>
            <a
              v-if="report.k6_script_available"
              :href="k6BundleUrl"
              download="k6.zip"
              class="btn-secondary text-xs"
            >
              <Download class="h-3.5 w-3.5" />
              <span>{{ t("perf.downloadBundle") }}</span>
            </a>
          </div>
        </div>

        <div class="space-y-4 p-5">
          <!-- Live k6 Metrics (when available) -->
          <template v-if="k6Normalized && k6Normalized.hasMetrics">
            <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div class="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
                <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  {{ t("perf.k6P95") }}
                </p>
                <p class="mt-1 text-xl font-semibold tabular-nums text-slate-900">
                  {{ k6Normalized.p95 }} ms
                </p>
                <p class="mt-0.5 text-[11px] text-slate-500">
                  {{ t("perf.k6P95Sub", { avg: k6Normalized.avg, p90: k6Normalized.p90 }) }}
                </p>
              </div>
              <div class="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
                <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  {{ t("perf.k6Rps") }}
                </p>
                <p class="mt-1 text-xl font-semibold tabular-nums text-indigo-700">
                  {{ k6Normalized.rps }} req/s
                </p>
                <p class="mt-0.5 text-[11px] text-slate-500">
                  {{ t("perf.k6ReqTotal", { count: k6Normalized.reqCount }) }}
                </p>
              </div>
              <div class="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
                <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  {{ t("perf.k6Rate429") }}
                </p>
                <p
                  class="mt-1 text-xl font-semibold tabular-nums"
                  :class="k6Normalized.rate429 > 0 ? 'text-amber-700' : 'text-emerald-700'"
                >
                  {{ (k6Normalized.rate429 * 100).toFixed(2) }}%
                </p>
                <p class="mt-0.5 text-[11px] text-slate-500">
                  {{ t("perf.k6Count429", { count: k6Normalized.count429 }) }}
                </p>
              </div>
              <div class="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5">
                <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  {{ t("perf.k6Rate5xx") }}
                </p>
                <p
                  class="mt-1 text-xl font-semibold tabular-nums"
                  :class="k6Normalized.rate5xx > 0 ? 'text-rose-700' : 'text-emerald-700'"
                >
                  {{ (k6Normalized.rate5xx * 100).toFixed(2) }}%
                </p>
                <p class="mt-0.5 text-[11px] text-slate-500">
                  {{ t("perf.k6Count5xx", { count: k6Normalized.count5xx }) }}
                </p>
              </div>
            </div>
          </template>

          <!-- Diagnosis Notes -->
          <div
            v-if="k6Normalized && (k6Normalized.notes.length > 0 || k6Normalized.breachedThresholds.length > 0)"
            class="rounded-xl border px-4 py-3 text-xs"
            :class="
              k6Normalized.inflectionDetected
                ? 'border-rose-200 bg-rose-50/70 text-rose-800'
                : 'border-slate-200 bg-slate-50/70 text-slate-700'
            "
          >
            <div class="flex items-center justify-between gap-2">
              <span class="font-semibold">{{ t("perf.k6NotesTitle") }}</span>
              <StatusBadge :status="k6Normalized.status" />
            </div>
            <ul class="mt-1.5 list-disc space-y-1 pl-4">
              <li v-for="(note, idx) in k6Normalized.notes" :key="idx">{{ note }}</li>
              <li
                v-for="(bt, idx) in k6Normalized.breachedThresholds"
                :key="`bt-${idx}`"
                class="font-mono"
              >
                {{ t("perf.k6ThresholdBreached", { expr: bt }) }}
              </li>
            </ul>
          </div>

          <!-- Copyable k6 CLI Command -->
          <div class="rounded-xl border border-slate-200 bg-slate-50/70 p-4">
            <div class="flex items-center justify-between gap-2">
              <span class="text-xs font-semibold text-slate-800">
                {{ t("perf.k6RunTitle") }}
              </span>
              <button
                type="button"
                class="btn-secondary px-2.5 py-1 text-xs"
                @click="copyK6Command"
              >
                <Check v-if="copiedCmd" class="h-3.5 w-3.5 text-emerald-600" />
                <Copy v-else class="h-3.5 w-3.5" />
                <span>{{ copiedCmd ? t("burp.copied") : t("perf.copyCmd") }}</span>
              </button>
            </div>
            <pre
              class="mt-2.5 overflow-x-auto rounded-lg bg-slate-950 p-3 font-mono text-xs leading-relaxed text-emerald-300"
            >{{ k6Command }}</pre>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
