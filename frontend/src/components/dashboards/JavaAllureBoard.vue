<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Code2,
  Copy,
  Download,
  ExternalLink,
  FileCode,
  Loader2,
  Play,
  RefreshCw,
  Search,
  Terminal,
} from "lucide-vue-next";
import { artifactDownloadUrl } from "@/api/client";
import { getRunJavaReport } from "@/api/reports";
import { executeRunModules, getRun } from "@/api/runs";
import type { AllureTestResult, JavaAllureReport } from "@/api/types";
import EmptyState from "@/components/layout/EmptyState.vue";
import MetricCard from "@/components/layout/MetricCard.vue";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import { useI18n } from "@/i18n";
import "@/i18n/locales/dashboards";

interface NormalizedTestCase {
  id: string;
  name: string;
  status: string;
  durationMs: number;
  epic: string;
  feature: string;
  story: string;
  message: string;
  stackTrace: string;
}

interface GroupedTests {
  groupKey: string;
  epic: string;
  feature: string;
  story: string;
  cases: NormalizedTestCase[];
}

const props = defineProps<{
  runId: string;
  projectId?: string;
}>();

const emit = defineEmits<{
  (e: "executed"): void;
}>();

const { locale, t } = useI18n();

const loading = ref(true);
const executing = ref(false);
const error = ref<string | null>(null);
const execError = ref<string | null>(null);
const report = ref<JavaAllureReport | null>(null);
const javaPackage = ref("com.reconqa.generated");
const javaVersion = ref("21");
const statusFilter = ref<string>("all");
const searchQuery = ref("");
const expandedRowIds = ref<Set<string>>(new Set());
const copiedCmd = ref<string | null>(null);
let isUnmounted = false;

const donutChartEl = ref<HTMLDivElement | null>(null);
let donutChart: echarts.ECharts | null = null;
let resizeObserver: ResizeObserver | null = null;

const javaZipUrl = computed(() => artifactDownloadUrl(props.runId, "java-tests"));

const mavenCommand = "mvn test && allure serve target/allure-results";
const dockerRunnerCommand =
  "docker compose --profile runner run --rm -e RECONQA_BASE_URI=https://api.example.com java-runner";

const normalizedTests = computed<NormalizedTestCase[]>(() => {
  const rep = report.value;
  if (!rep) return [];

  if (Array.isArray(rep.allure_results) && rep.allure_results.length > 0) {
    return rep.allure_results.map((item: AllureTestResult, idx: number) => ({
      id: `allure-${idx}-${item.name}`,
      name: item.name || `Test #${idx + 1}`,
      status: String(item.status || "unknown").toLowerCase(),
      durationMs: Number(item.duration_ms ?? 0) || 0,
      epic: item.epic || "ReconQA Suite",
      feature: item.feature || "General",
      story: item.story || "Default",
      message: item.message || "",
      stackTrace: typeof item.stackTrace === "string" ? item.stackTrace : "",
    }));
  }

  const exec = rep.execution_summary;
  if (exec && Array.isArray(exec.tests)) {
    return (exec.tests as Array<Record<string, unknown>>).map((item, idx) => ({
      id: `exec-${idx}-${String(item.displayName ?? item.testMethod ?? idx)}`,
      name: String(item.displayName ?? item.testMethod ?? `Test #${idx + 1}`),
      status: String(item.status ?? "unknown").toLowerCase(),
      durationMs: Number(item.durationMs ?? item.duration_ms ?? 0) || 0,
      epic: String(item.epic || "ReconQA Suite"),
      feature: String(item.feature || "General"),
      story: String(item.story || "Default"),
      message: String(item.failureMessage ?? item.message ?? ""),
      stackTrace: String(item.stackTrace ?? ""),
    }));
  }

  return [];
});

const hasExecutionData = computed(() => {
  const rep = report.value;
  if (!rep) return false;
  return Boolean(rep.execution_summary) || normalizedTests.value.length > 0;
});

const statusCounts = computed(() => {
  const exec = report.value?.execution_summary;
  let passed = 0;
  let failed = 0;
  let broken = 0;
  let skipped = 0;

  if (normalizedTests.value.length > 0) {
    for (const tc of normalizedTests.value) {
      if (tc.status === "passed") passed += 1;
      else if (tc.status === "failed") failed += 1;
      else if (tc.status === "broken") broken += 1;
      else skipped += 1;
    }
  } else if (exec) {
    passed = Number(exec.passed ?? 0);
    failed = Number(exec.failed ?? 0);
    broken = Number(exec.broken ?? 0);
    skipped = Number(exec.skipped ?? 0);
  }

  const total = passed + failed + broken + skipped;
  const passRate = total > 0 ? Math.round((passed / total) * 1000) / 10 : 0;
  return { total, passed, failed, broken, skipped, passRate };
});

const executionDurationMs = computed(() => {
  const exec = report.value?.execution_summary;
  if (exec) {
    const raw = exec.durationMs ?? exec.duration_ms;
    if (typeof raw === "number") return raw;
  }
  return normalizedTests.value.reduce((acc, item) => acc + item.durationMs, 0);
});

const filteredTests = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  return normalizedTests.value.filter((tc) => {
    if (statusFilter.value !== "all" && tc.status !== statusFilter.value) {
      return false;
    }
    if (!q) return true;
    return (
      tc.name.toLowerCase().includes(q) ||
      tc.epic.toLowerCase().includes(q) ||
      tc.feature.toLowerCase().includes(q) ||
      tc.story.toLowerCase().includes(q) ||
      tc.message.toLowerCase().includes(q)
    );
  });
});

const groupedTests = computed<GroupedTests[]>(() => {
  const map = new Map<string, GroupedTests>();
  for (const tc of filteredTests.value) {
    const key = `${tc.epic}:::${tc.feature}:::${tc.story}`;
    const existing = map.get(key);
    if (existing) {
      existing.cases.push(tc);
    } else {
      map.set(key, {
        groupKey: key,
        epic: tc.epic,
        feature: tc.feature,
        story: tc.story,
        cases: [tc],
      });
    }
  }
  return Array.from(map.values());
});

const schemathesisStatus = computed(() => {
  const sch = report.value?.schemathesis;
  if (!sch) return "not_run";
  return String(sch.status ?? "unknown");
});

const schemathesisDisplayStatus = computed(() => {
  if (schemathesisStatus.value === "not_run") return t("java.statusNotRun");
  if (schemathesisStatus.value === "passed") return t("java.passed");
  if (schemathesisStatus.value === "failed") return t("java.failed");
  if (schemathesisStatus.value === "skipped") return t("java.skipped");
  return schemathesisStatus.value.replace(/_/g, " ");
});

const schemathesisSummary = computed(() => {
  const sch = report.value?.schemathesis;
  if (!sch || typeof sch.summary !== "object" || sch.summary === null) return null;
  const s = sch.summary as Record<string, unknown>;
  const passed = Number(s.passed ?? s.checks_passed ?? 0);
  const failed = Number(s.failed ?? s.checks_failed ?? 0);
  const errors = Number(s.errors ?? s.server_errors ?? 0);
  const skipped = Number(s.skipped ?? 0);
  const total = Number(s.total ?? passed + failed + errors + skipped);
  return {
    total,
    passed,
    failed,
    errors,
    skipped,
  };
});

const statusFilterOptions = computed(() => [
  { key: "all", label: t("java.all") },
  { key: "passed", label: t("java.passed") },
  { key: "failed", label: t("java.failed") },
  { key: "broken", label: t("java.broken") },
  { key: "skipped", label: t("java.skipped") },
]);

function toggleRow(id: string): void {
  const next = new Set(expandedRowIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  expandedRowIds.value = next;
}

async function copyCommand(key: string, cmd: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(cmd);
    copiedCmd.value = key;
    window.setTimeout(() => {
      if (copiedCmd.value === key) copiedCmd.value = null;
    }, 1800);
  } catch {
    // Ignore clipboard error
  }
}

function formatDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${Math.round(ms)} ms`;
}

function renderDonutChart(): void {
  if (!donutChartEl.value || !hasExecutionData.value) return;

  if (!donutChart) {
    donutChart = echarts.init(donutChartEl.value);
    resizeObserver = new ResizeObserver(() => {
      donutChart?.resize();
    });
    resizeObserver.observe(donutChartEl.value);
  }

  const c = statusCounts.value;
  donutChart.setOption(
    {
      tooltip: {
        trigger: "item",
        formatter: "{b}: <b>{c}</b> ({d}%)",
      },
      legend: {
        bottom: 0,
        icon: "circle",
        itemWidth: 10,
        itemHeight: 10,
        textStyle: { color: "#475569", fontSize: 12 },
      },
      title: {
        text: `${c.passRate}%`,
        subtext: t("java.chartTestsSub", { count: c.total }),
        left: "center",
        top: "38%",
        textStyle: {
          fontSize: 22,
          fontWeight: 700,
          color: "#0f172a",
        },
        subtextStyle: {
          fontSize: 12,
          color: "#64748b",
        },
      },
      series: [
        {
          name: t("java.chartSeriesName"),
          type: "pie",
          radius: ["56%", "76%"],
          center: ["50%", "45%"],
          avoidLabelOverlap: true,
          itemStyle: {
            borderRadius: 6,
            borderColor: "#ffffff",
            borderWidth: 2,
          },
          label: { show: false },
          data: [
            { value: c.passed, name: t("java.passed"), itemStyle: { color: "#10b981" } },
            { value: c.failed, name: t("java.failed"), itemStyle: { color: "#f43f5e" } },
            { value: c.broken, name: t("java.broken"), itemStyle: { color: "#f59e0b" } },
            { value: c.skipped, name: t("java.skipped"), itemStyle: { color: "#94a3b8" } },
          ],
        },
      ],
    },
    true
  );
}

function handleWindowResize(): void {
  donutChart?.resize();
}

function disposeChart(): void {
  resizeObserver?.disconnect();
  resizeObserver = null;
  donutChart?.dispose();
  donutChart = null;
}

async function loadJavaReport(): Promise<void> {
  if (!props.runId) return;
  loading.value = true;
  error.value = null;
  disposeChart();
  try {
    const res = await getRunJavaReport(props.runId);
    report.value = res;
    if (res.project_meta?.package_name) {
      javaPackage.value = res.project_meta.package_name;
    }
    if (res.project_meta?.java_version) {
      javaVersion.value = res.project_meta.java_version;
    }
  } catch (err) {
    report.value = null;
    error.value = err instanceof Error ? err.message : t("java.errorFallback");
  } finally {
    loading.value = false;
  }

  if (report.value && report.value.generated && hasExecutionData.value) {
    await nextTick();
    renderDonutChart();
  }
}

async function handleGenerateJava(): Promise<void> {
  if (!props.runId || executing.value) return;
  executing.value = true;
  execError.value = null;
  try {
    await executeRunModules(props.runId, {
      modules: ["java_gen"],
      config: {
        java_package: javaPackage.value.trim() || "com.reconqa.generated",
        java_version: javaVersion.value || "21",
      },
    });
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
      execError.value = t("java.execFailed");
    }
    await loadJavaReport();
    emit("executed");
  } catch (err) {
    execError.value = err instanceof Error ? err.message : t("java.execFailed");
  } finally {
    executing.value = false;
  }
}

watch(
  () => props.runId,
  () => {
    expandedRowIds.value = new Set();
    execError.value = null;
    void loadJavaReport();
  }
);

watch(locale, () => {
  renderDonutChart();
});

onMounted(() => {
  isUnmounted = false;
  window.addEventListener("resize", handleWindowResize);
  void loadJavaReport();
});

onBeforeUnmount(() => {
  isUnmounted = true;
  window.removeEventListener("resize", handleWindowResize);
  disposeChart();
});
</script>

<template>
  <div class="space-y-5">
    <!-- Contextual On-Demand Generation Bar -->
    <div class="card p-4">
      <div class="flex flex-wrap items-end justify-between gap-4">
        <div class="min-w-0">
          <h3 class="text-sm font-semibold text-slate-900">{{ t("java.actionTitle") }}</h3>
          <p class="mt-0.5 text-xs text-slate-500">{{ t("java.actionDesc") }}</p>
        </div>
        <div class="flex flex-wrap items-end gap-2.5">
          <div>
            <label class="block text-[11px] font-medium text-slate-600">
              {{ t("java.packageLabel") }}
            </label>
            <input
              v-model="javaPackage"
              type="text"
              data-testid="java-package-input"
              class="input-field mt-1 w-56 py-1.5 font-mono text-xs"
              placeholder="com.reconqa.generated"
              :disabled="executing"
            />
          </div>
          <div>
            <label class="block text-[11px] font-medium text-slate-600">
              {{ t("java.versionLabel") }}
            </label>
            <select
              v-model="javaVersion"
              data-testid="java-version-select"
              class="input-field mt-1 w-24 py-1.5 text-xs"
              :disabled="executing"
            >
              <option value="21">JDK 21</option>
              <option value="17">JDK 17</option>
              <option value="8">JDK 8</option>
            </select>
          </div>
          <button
            type="button"
            data-testid="java-generate-btn"
            class="btn-primary py-1.5 text-xs"
            :disabled="executing"
            @click="handleGenerateJava"
          >
            <Loader2 v-if="executing" class="h-3.5 w-3.5 animate-spin" />
            <Play v-else class="h-3.5 w-3.5" />
            <span>
              {{
                executing
                  ? t("java.generatingBtn")
                  : report?.generated
                    ? t("java.regenerateBtn")
                    : t("java.generateBtn")
              }}
            </span>
          </button>
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
      <span>{{ t("java.loading") }}</span>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="card border-rose-200 bg-rose-50/70 p-6">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h3 class="text-sm font-semibold text-rose-800">{{ t("java.errorTitle") }}</h3>
          <p class="mt-1 text-xs text-rose-700">{{ error }}</p>
        </div>
        <button type="button" class="btn-secondary text-xs" @click="loadJavaReport">
          <RefreshCw class="h-3.5 w-3.5" />
          <span>{{ t("java.retry") }}</span>
        </button>
      </div>
    </div>

    <!-- Not generated empty state -->
    <EmptyState
      v-else-if="!report || !report.generated"
      :title="t('java.emptyTitle')"
      :description="t('java.emptyDesc')"
    >
      <template #icon>
        <Code2 class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <button type="button" class="btn-secondary text-xs" @click="loadJavaReport">
          <RefreshCw class="h-3.5 w-3.5" />
          <span>{{ t("java.refresh") }}</span>
        </button>
      </template>
    </EmptyState>

    <!-- Main Dashboard -->
    <template v-else>
      <!-- Top Action Bar -->
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-2.5">
          <StatusBadge status="completed" size="md" />
          <span class="rounded-md bg-slate-100 px-2.5 py-1 font-mono text-xs text-slate-700">
            {{ report.project_meta.package_name || "com.reconqa.generated" }}
          </span>
          <span class="rounded-md bg-indigo-50 px-2.5 py-1 font-mono text-xs font-medium text-indigo-700">
            JDK {{ report.project_meta.java_version || "21" }}
          </span>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <a
            v-if="report.allure_report_url"
            :href="report.allure_report_url"
            target="_blank"
            rel="noopener noreferrer"
            class="btn-secondary text-xs"
          >
            <ExternalLink class="h-3.5 w-3.5" />
            <span>{{ t("java.openAllureHtml") }}</span>
          </a>
          <a :href="javaZipUrl" download="java-tests.zip" class="btn-primary text-xs">
            <Download class="h-3.5 w-3.5" />
            <span>{{ t("java.downloadZip") }}</span>
          </a>
        </div>
      </div>

      <!-- Summary KPIs -->
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <MetricCard
          :label="t('java.kpiMavenProject')"
          :value="t('java.kpiMavenReady')"
          :subtitle="t('java.kpiMavenSub', { version: report.project_meta.java_version || '21' })"
          tone="emerald"
        />
        <MetricCard
          :label="t('java.kpiL1')"
          :value="report.project_meta.l1_cases ?? 0"
          :subtitle="t('java.kpiL1Sub')"
          tone="indigo"
        />
        <MetricCard
          :label="t('java.kpiL2')"
          :value="report.project_meta.l2_chains ?? 0"
          :subtitle="t('java.kpiL2Sub')"
          tone="indigo"
        />
        <MetricCard
          :label="t('java.kpiL3')"
          :value="report.project_meta.l3_checks ?? 0"
          :subtitle="t('java.kpiL3Sub')"
          tone="amber"
        />
        <MetricCard
          :label="t('java.kpiSchemathesis')"
          :value="schemathesisDisplayStatus"
          :subtitle="
            schemathesisSummary
              ? t('java.kpiSchemathesisPass', {
                  passed: schemathesisSummary.passed,
                  total: schemathesisSummary.total,
                })
              : t('java.kpiSchemathesisSub')
          "
          :tone="
            schemathesisStatus === 'passed'
              ? 'emerald'
              : schemathesisStatus === 'failed'
                ? 'rose'
                : 'neutral'
          "
        />
      </div>

      <!-- Execution Board (When execution_summary or allure_results exist) -->
      <div v-if="hasExecutionData" class="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <!-- Donut Chart Card -->
        <div class="card flex flex-col lg:col-span-4">
          <div class="card-header">
            <div>
              <h3 class="text-sm font-semibold text-slate-900">{{ t("java.execTitle") }}</h3>
              <p class="text-xs text-slate-500">
                {{ t("java.totalDuration", { duration: formatDuration(executionDurationMs) }) }}
              </p>
            </div>
            <StatusBadge
              :status="statusCounts.failed > 0 || statusCounts.broken > 0 ? 'failed' : 'passed'"
            />
          </div>
          <div class="flex flex-1 flex-col justify-between p-5">
            <div ref="donutChartEl" class="h-64 w-full" />
            <div class="mt-4 grid grid-cols-4 gap-2 border-t border-slate-100 pt-3 text-center">
              <div>
                <p class="text-[11px] font-medium uppercase text-slate-400">{{ t("java.passed") }}</p>
                <p class="mt-0.5 text-base font-semibold tabular-nums text-emerald-600">
                  {{ statusCounts.passed }}
                </p>
              </div>
              <div>
                <p class="text-[11px] font-medium uppercase text-slate-400">{{ t("java.failed") }}</p>
                <p class="mt-0.5 text-base font-semibold tabular-nums text-rose-600">
                  {{ statusCounts.failed }}
                </p>
              </div>
              <div>
                <p class="text-[11px] font-medium uppercase text-slate-400">{{ t("java.broken") }}</p>
                <p class="mt-0.5 text-base font-semibold tabular-nums text-amber-600">
                  {{ statusCounts.broken }}
                </p>
              </div>
              <div>
                <p class="text-[11px] font-medium uppercase text-slate-400">{{ t("java.skipped") }}</p>
                <p class="mt-0.5 text-base font-semibold tabular-nums text-slate-500">
                  {{ statusCounts.skipped }}
                </p>
              </div>
            </div>
          </div>
        </div>

        <!-- Grouped Test Cases Table -->
        <div class="card flex flex-col overflow-hidden lg:col-span-8">
          <div class="card-header flex-wrap">
            <div>
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("java.casesTitle") }}
              </h3>
              <p class="text-xs text-slate-500">
                {{ t("java.casesCount", { shown: filteredTests.length, total: normalizedTests.length }) }}
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <div class="relative">
                <Search class="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
                <input
                  v-model="searchQuery"
                  type="text"
                  :placeholder="t('java.filterPlaceholder')"
                  class="input-field py-1 pl-8 pr-3 text-xs"
                />
              </div>
              <div class="flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-xs">
                <button
                  v-for="opt in statusFilterOptions"
                  :key="opt.key"
                  type="button"
                  class="rounded-md px-2 py-1 transition"
                  :class="
                    statusFilter === opt.key
                      ? 'bg-white font-medium text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="statusFilter = opt.key"
                >
                  {{ opt.label }}
                </button>
              </div>
            </div>
          </div>

          <div class="max-h-[460px] overflow-auto">
            <div v-if="groupedTests.length === 0" class="py-12 text-center text-xs text-slate-500">
              {{ t("java.noMatchCases") }}
            </div>
            <div v-else class="divide-y divide-slate-200">
              <div v-for="grp in groupedTests" :key="grp.groupKey">
                <div class="flex flex-wrap items-center gap-2 bg-slate-50/90 px-4 py-2 text-xs">
                  <span class="font-semibold text-slate-800">{{ grp.epic }}</span>
                  <span class="text-slate-300">/</span>
                  <span class="font-medium text-indigo-700">{{ grp.feature }}</span>
                  <span class="text-slate-300">/</span>
                  <span class="text-slate-600">{{ grp.story }}</span>
                  <span class="ml-auto rounded-full bg-slate-200/70 px-2 py-0.5 text-[11px] font-medium text-slate-600">
                    {{ grp.cases.length }}
                  </span>
                </div>

                <table class="min-w-full divide-y divide-slate-100 text-xs">
                  <tbody class="divide-y divide-slate-100 bg-white">
                    <template v-for="tc in grp.cases" :key="tc.id">
                      <tr
                        class="transition hover:bg-slate-50/80"
                        :class="tc.message || tc.stackTrace ? 'cursor-pointer' : ''"
                        @click="tc.message || tc.stackTrace ? toggleRow(tc.id) : undefined"
                      >
                        <td class="w-8 py-2.5 pl-4 pr-1 text-slate-400">
                          <template v-if="tc.message || tc.stackTrace">
                            <ChevronDown v-if="expandedRowIds.has(tc.id)" class="h-3.5 w-3.5" />
                            <ChevronRight v-else class="h-3.5 w-3.5" />
                          </template>
                        </td>
                        <td class="py-2.5 pr-3 font-mono text-slate-800">
                          {{ tc.name }}
                        </td>
                        <td class="w-28 py-2.5 pr-3">
                          <StatusBadge :status="tc.status" />
                        </td>
                        <td class="w-24 py-2.5 pr-4 text-right font-mono tabular-nums text-slate-500">
                          {{ formatDuration(tc.durationMs) }}
                        </td>
                      </tr>
                      <tr
                        v-if="(tc.message || tc.stackTrace) && expandedRowIds.has(tc.id)"
                        class="bg-rose-50/40"
                      >
                        <td colspan="4" class="px-6 py-3">
                          <p v-if="tc.message" class="font-mono text-xs font-medium text-rose-800">
                            {{ tc.message }}
                          </p>
                          <pre
                            v-if="tc.stackTrace"
                            class="mt-2 max-h-44 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200"
                          >{{ tc.stackTrace }}</pre>
                        </td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Not Executed Yet: Ready-to-copy Maven & Docker Runner Commands -->
      <div v-else class="card overflow-hidden">
        <div class="card-header">
          <div class="flex items-center gap-2.5">
            <Play class="h-4 w-4 text-indigo-600" />
            <div>
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("java.execGuideTitle") }}
              </h3>
              <p class="text-xs text-slate-500">
                {{ t("java.execGuideDesc") }}
              </p>
            </div>
          </div>
          <a :href="javaZipUrl" download="java-tests.zip" class="btn-primary text-xs">
            <Download class="h-3.5 w-3.5" />
            <span>{{ t("java.downloadZip") }}</span>
          </a>
        </div>

        <div class="grid grid-cols-1 gap-4 p-5 md:grid-cols-2">
          <!-- Local Maven + Allure -->
          <div class="rounded-xl border border-slate-200 bg-slate-50/70 p-4">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2 text-xs font-semibold text-slate-800">
                <Terminal class="h-4 w-4 text-indigo-600" />
                <span>{{ t("java.localMvnTitle") }}</span>
              </div>
              <button
                type="button"
                class="btn-secondary px-2.5 py-1 text-xs"
                @click="copyCommand('mvn', mavenCommand)"
              >
                <Check v-if="copiedCmd === 'mvn'" class="h-3.5 w-3.5 text-emerald-600" />
                <Copy v-else class="h-3.5 w-3.5" />
                <span>{{ copiedCmd === "mvn" ? t("burp.copied") : t("burp.copy") }}</span>
              </button>
            </div>
            <pre
              class="mt-3 overflow-x-auto rounded-lg bg-slate-950 p-3 font-mono text-xs text-emerald-300"
            >{{ mavenCommand }}</pre>
            <p class="mt-2 text-xs text-slate-500">
              {{ t("java.localMvnHint") }}
            </p>
          </div>

          <!-- Docker java-runner -->
          <div class="rounded-xl border border-slate-200 bg-slate-50/70 p-4">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2 text-xs font-semibold text-slate-800">
                <FileCode class="h-4 w-4 text-indigo-600" />
                <span>{{ t("java.dockerRunnerTitle") }}</span>
              </div>
              <button
                type="button"
                class="btn-secondary px-2.5 py-1 text-xs"
                @click="copyCommand('docker', dockerRunnerCommand)"
              >
                <Check v-if="copiedCmd === 'docker'" class="h-3.5 w-3.5 text-emerald-600" />
                <Copy v-else class="h-3.5 w-3.5" />
                <span>{{ copiedCmd === "docker" ? t("burp.copied") : t("burp.copy") }}</span>
              </button>
            </div>
            <pre
              class="mt-3 overflow-x-auto rounded-lg bg-slate-950 p-3 font-mono text-xs text-sky-300"
            >{{ dockerRunnerCommand }}</pre>
            <p class="mt-2 text-xs text-slate-500">
              {{ t("java.dockerRunnerHint") }}
            </p>
          </div>
        </div>
      </div>

      <!-- Schemathesis Summary Card (when present) -->
      <div v-if="report.schemathesis" class="card overflow-hidden">
        <div class="card-header">
          <div>
            <h3 class="text-sm font-semibold text-slate-900">
              {{ t("java.schemathesisTitle") }}
            </h3>
            <p v-if="typeof report.schemathesis.reason === 'string'" class="text-xs text-slate-500">
              {{ report.schemathesis.reason }}
            </p>
          </div>
          <StatusBadge :status="schemathesisStatus" />
        </div>
        <div v-if="schemathesisSummary" class="grid grid-cols-2 gap-3 p-4 sm:grid-cols-5">
          <div class="rounded-lg bg-slate-50 px-3 py-2 text-center">
            <p class="text-[11px] uppercase text-slate-400">{{ t("java.schemathesisTotal") }}</p>
            <p class="text-sm font-semibold tabular-nums text-slate-800">
              {{ schemathesisSummary.total }}
            </p>
          </div>
          <div class="rounded-lg bg-emerald-50/70 px-3 py-2 text-center">
            <p class="text-[11px] uppercase text-emerald-600">{{ t("java.passed") }}</p>
            <p class="text-sm font-semibold tabular-nums text-emerald-700">
              {{ schemathesisSummary.passed }}
            </p>
          </div>
          <div class="rounded-lg bg-rose-50/70 px-3 py-2 text-center">
            <p class="text-[11px] uppercase text-rose-600">{{ t("java.failed") }}</p>
            <p class="text-sm font-semibold tabular-nums text-rose-700">
              {{ schemathesisSummary.failed }}
            </p>
          </div>
          <div class="rounded-lg bg-amber-50/70 px-3 py-2 text-center">
            <p class="text-[11px] uppercase text-amber-600">{{ t("java.schemathesisErrors") }}</p>
            <p class="text-sm font-semibold tabular-nums text-amber-700">
              {{ schemathesisSummary.errors }}
            </p>
          </div>
          <div class="rounded-lg bg-slate-50 px-3 py-2 text-center">
            <p class="text-[11px] uppercase text-slate-400">{{ t("java.skipped") }}</p>
            <p class="text-sm font-semibold tabular-nums text-slate-600">
              {{ schemathesisSummary.skipped }}
            </p>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
