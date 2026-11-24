<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Check,
  CheckCircle2,
  Code2,
  Compass,
  Container,
  Copy,
  Download,
  FileUp,
  FolderKanban,
  Gauge,
  GitBranch,
  Globe,
  Info,
  Layers,
  Loader2,
  PlayCircle,
  Plus,
  RefreshCw,
  ShieldCheck,
  Terminal,
  Wrench,
  X,
  XCircle,
} from "lucide-vue-next";
import { listProjects, listTargets } from "@/api/projects";
import { getRunTitle, listProjectRuns, shortRunId } from "@/api/runs";
import { getDoctorStatus, installDoctorTool } from "@/api/reports";
import type { DoctorInstallResult, DoctorToolStatus, Project, Run, Target } from "@/api/types";
import { useI18n } from "@/i18n";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import EmptyState from "@/components/layout/EmptyState.vue";

interface EnrichedRun extends Run {
  projectName: string;
}

interface ProjectWorkspaceCard {
  project: Project;
  primaryDomain: string | null;
  latestRun: Run | null;
  runCount: number;
  endpoints: number | null;
  coveragePct: number | null;
  highCritical: number | null;
}

interface DoctorEntry extends DoctorToolStatus {
  name: string;
}

const CORE_TOOL_ORDER = ["mvn", "allure", "k6", "mitmdump", "nuclei", "java", "docker"];
const CORE_TOOL_SET = new Set(CORE_TOOL_ORDER);

const { locale, t } = useI18n();

const loading = ref(true);
const doctorLoading = ref(true);
const error = ref<string | null>(null);
const doctorError = ref<string | null>(null);

const projects = ref<Project[]>([]);
const runsByProject = ref<Record<string, Run[]>>({});
const targetsByProject = ref<Record<string, Target[]>>({});
const recentRuns = ref<EnrichedRun[]>([]);
const doctorTools = ref<Record<string, DoctorToolStatus>>({});

// Tool Setup Modal state
const selectedToolName = ref<string | null>(null);
const copiedField = ref<"install" | "docker" | null>(null);
const installingTool = ref(false);
const installResult = ref<DoctorInstallResult | null>(null);
const installError = ref<string | null>(null);

const projectCards = computed<ProjectWorkspaceCard[]>(() =>
  projects.value.map((project) => {
    const pRuns = runsByProject.value[project.id] ?? [];
    const pTargets = targetsByProject.value[project.id] ?? [];
    const latestRun = pRuns[0] ?? null;
    const metrics = latestRun?.summary_metrics;
    const endpoints =
      typeof metrics?.total_endpoints === "number" ? metrics.total_endpoints : null;
    const coveragePct =
      typeof metrics?.coverage_pct === "number" ? metrics.coverage_pct : null;
    const bySev = metrics?.findings_by_severity;
    const highCritical = latestRun
      ? Number(bySev?.critical ?? 0) + Number(bySev?.high ?? 0)
      : null;

    return {
      project,
      primaryDomain: pTargets[0]?.root_domain ?? null,
      latestRun,
      runCount: pRuns.length,
      endpoints,
      coveragePct,
      highCritical,
    };
  })
);

const doctorEntries = computed<DoctorEntry[]>(() =>
  Object.entries(doctorTools.value).map(([name, info]) => ({
    name,
    ...info,
  }))
);

const coreExecutionTools = computed<DoctorEntry[]>(() => {
  const list = doctorEntries.value.filter((tItem) => CORE_TOOL_SET.has(tItem.name));
  list.sort((a, b) => CORE_TOOL_ORDER.indexOf(a.name) - CORE_TOOL_ORDER.indexOf(b.name));
  return list;
});

const optionalDiscoveryTools = computed<DoctorEntry[]>(() =>
  doctorEntries.value.filter((tItem) => !CORE_TOOL_SET.has(tItem.name))
);

const doctorAvailableCount = computed(
  () => doctorEntries.value.filter((tItem) => tItem.available).length
);

const coreAvailableCount = computed(
  () => coreExecutionTools.value.filter((tItem) => tItem.available).length
);

const selectedTool = computed<DoctorEntry | null>(() => {
  if (!selectedToolName.value) return null;
  const info = doctorTools.value[selectedToolName.value];
  if (!info) return null;
  return { name: selectedToolName.value, ...info };
});

const selectedToolPurpose = computed(() => {
  const tool = selectedTool.value;
  if (!tool) return "";
  const localized = locale.value === "zh" ? tool.purpose_zh : tool.purpose_en;
  return (
    localized ||
    tool.purpose_en ||
    tool.purpose_zh ||
    t("overview.doctor.modal.defaultPurpose", { category: tool.category })
  );
});

const selectedToolImpact = computed(() => {
  const tool = selectedTool.value;
  if (!tool) return "";
  const localized = locale.value === "zh" ? tool.impact_zh : tool.impact_en;
  return (
    localized ||
    tool.impact_en ||
    tool.impact_zh ||
    t("overview.doctor.modal.defaultImpact")
  );
});

const selectedToolInstallCmd = computed(() => {
  const tool = selectedTool.value;
  if (!tool) return "";
  return tool.install_cmd || tool.install_hint || "";
});

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

function openToolModal(toolName: string): void {
  selectedToolName.value = toolName;
  copiedField.value = null;
  installResult.value = null;
  installError.value = null;
}

function closeToolModal(): void {
  if (installingTool.value) return;
  selectedToolName.value = null;
  copiedField.value = null;
  installResult.value = null;
  installError.value = null;
}

async function copyCommand(text: string, field: "install" | "docker"): Promise<void> {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    copiedField.value = field;
    window.setTimeout(() => {
      if (copiedField.value === field) {
        copiedField.value = null;
      }
    }, 2000);
  } catch {
    copiedField.value = field;
  }
}

async function handleAutoInstall(): Promise<void> {
  const tool = selectedTool.value;
  if (!tool || installingTool.value) return;
  installingTool.value = true;
  installResult.value = null;
  installError.value = null;
  try {
    const res = await installDoctorTool(tool.name);
    installResult.value = res;
    if (res.doctor && typeof res.doctor === "object") {
      doctorTools.value = res.doctor;
    } else {
      await loadDoctor();
    }
  } catch (err) {
    installError.value = err instanceof Error ? err.message : t("overview.err.doctor");
  } finally {
    installingTool.value = false;
  }
}

function onKeyDown(e: KeyboardEvent): void {
  if (e.key === "Escape" && selectedToolName.value) {
    closeToolModal();
  }
}

async function loadOverview(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const projectList = await listProjects();
    projects.value = projectList;

    const projectNameById = new Map(projectList.map((p) => [p.id, p.name]));
    const scopedProjects = projectList.slice(0, 12);

    const [runLists, targetLists] = await Promise.all([
      Promise.all(scopedProjects.map((p) => listProjectRuns(p.id).catch(() => [] as Run[]))),
      Promise.all(scopedProjects.map((p) => listTargets(p.id).catch(() => [] as Target[]))),
    ]);

    const nextRunsByProject: Record<string, Run[]> = {};
    const nextTargetsByProject: Record<string, Target[]> = {};
    const merged: EnrichedRun[] = [];

    scopedProjects.forEach((p, idx) => {
      const sortedRuns = [...(runLists[idx] ?? [])].sort((a, b) => {
        const ta = a.started_at ? new Date(a.started_at).getTime() : 0;
        const tb = b.started_at ? new Date(b.started_at).getTime() : 0;
        return tb - ta;
      });
      nextRunsByProject[p.id] = sortedRuns;
      nextTargetsByProject[p.id] = targetLists[idx] ?? [];
      for (const r of sortedRuns) {
        merged.push({
          ...r,
          projectName: projectNameById.get(r.project_id) ?? r.project_id.slice(0, 8),
        });
      }
    });

    runsByProject.value = nextRunsByProject;
    targetsByProject.value = nextTargetsByProject;

    merged.sort((a, b) => {
      const ta = a.started_at ? new Date(a.started_at).getTime() : 0;
      const tb = b.started_at ? new Date(b.started_at).getTime() : 0;
      return tb - ta;
    });

    recentRuns.value = merged;
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("overview.err.load");
  } finally {
    loading.value = false;
  }
}

async function loadDoctor(): Promise<void> {
  doctorLoading.value = true;
  doctorError.value = null;
  try {
    doctorTools.value = await getDoctorStatus();
  } catch (err) {
    doctorError.value = err instanceof Error ? err.message : t("overview.err.doctor");
  } finally {
    doctorLoading.value = false;
  }
}

async function refreshAll(): Promise<void> {
  await Promise.all([loadOverview(), loadDoctor()]);
}

onMounted(() => {
  void refreshAll();
  window.addEventListener("keydown", onKeyDown);
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeyDown);
});
</script>

<template>
  <div class="space-y-6">
    <!-- Hero Banner: Manual Testing / HAR Input -> Unified Hub -> 3 Core Capabilities -->
    <section
      class="relative overflow-hidden rounded-2xl border border-slate-800 bg-gradient-to-br from-slate-900 via-slate-900 to-indigo-950 p-6 text-white shadow-md"
    >
      <div class="flex flex-col justify-between gap-5 lg:flex-row lg:items-center">
        <div class="max-w-2xl space-y-2">
          <div
            class="inline-flex items-center gap-2 rounded-full bg-indigo-500/20 px-3 py-1 text-xs font-medium text-indigo-200 ring-1 ring-inset ring-indigo-400/30"
          >
            <Layers class="h-3.5 w-3.5" />
            <span>{{ t("overview.heroPill") }}</span>
          </div>
          <h2 class="text-2xl font-bold tracking-tight text-white">
            {{ t("overview.heroTitle") }}
          </h2>
          <p class="text-sm leading-relaxed text-slate-300">
            {{ t("overview.heroDesc") }}
          </p>
        </div>

        <div class="flex shrink-0 flex-wrap items-center gap-2.5">
          <router-link
            :to="{ name: 'runs', query: { new: '1' } }"
            class="inline-flex items-center gap-2 rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-400"
          >
            <FileUp class="h-4 w-4" />
            <span>{{ t("overview.ctaImport") }}</span>
          </router-link>
          <router-link
            to="/explore"
            class="inline-flex items-center gap-2 rounded-lg border border-sky-400/40 bg-sky-500/15 px-4 py-2.5 text-sm font-medium text-sky-100 transition hover:bg-sky-500/25"
            data-testid="overview-explore-cta"
          >
            <Compass class="h-4 w-4 text-sky-300" />
            <span>{{ t("overview.ctaExplore") }}</span>
          </router-link>
          <router-link
            to="/projects"
            class="inline-flex items-center gap-2 rounded-lg border border-slate-700 bg-slate-800/80 px-4 py-2.5 text-sm font-medium text-slate-100 transition hover:bg-slate-700"
          >
            <FolderKanban class="h-4 w-4" />
            <span>{{ t("overview.ctaProjects") }}</span>
          </router-link>
        </div>
      </div>

      <!-- Visual User Journey: Input -> Hub -> 3 Core Capabilities -->
      <div class="mt-6 grid grid-cols-1 gap-3 border-t border-slate-800/80 pt-5 lg:grid-cols-12">
        <!-- Input -->
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

        <!-- Unified Hub -->
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

        <!-- 3 Core Testing Capabilities -->
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

    <!-- Error banner -->
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

    <!-- Per-Project Status Card Grid (Isolated metrics per project) -->
    <section class="card overflow-hidden" data-testid="overview-project-cards">
      <div class="card-header">
        <div class="flex items-center gap-2.5">
          <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <FolderKanban class="h-4 w-4" />
          </div>
          <div>
            <div class="flex items-center gap-2">
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("overview.projects.title") }}
              </h3>
              <span
                class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600"
              >
                {{ projects.length }}
              </span>
            </div>
            <p class="text-xs text-slate-500">
              {{ t("overview.projects.subtitle") }}
            </p>
          </div>
        </div>

        <div class="flex items-center gap-2">
          <button
            type="button"
            class="btn-secondary !px-2.5 !py-1.5 !text-xs"
            :disabled="loading"
            @click="loadOverview"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
            <span>{{ t("common.refresh") }}</span>
          </button>
          <router-link to="/projects" class="btn-primary !px-2.5 !py-1.5 !text-xs">
            <Plus class="h-3.5 w-3.5" />
            <span>{{ t("overview.projects.newProject") }}</span>
          </router-link>
        </div>
      </div>

      <div v-if="loading" class="p-6 text-center text-sm text-slate-500">
        {{ t("overview.projects.loading") }}
      </div>

      <div v-else-if="projectCards.length === 0" class="p-6">
        <EmptyState
          :title="t('overview.projects.emptyTitle')"
          :description="t('overview.projects.emptyDesc')"
        >
          <template #icon>
            <FolderKanban class="h-5 w-5" />
          </template>
          <template #action>
            <router-link to="/projects" class="btn-secondary">
              {{ t("overview.projects.newProject") }}
            </router-link>
            <router-link :to="{ name: 'runs', query: { new: '1' } }" class="btn-primary">
              {{ t("overview.ctaImport") }}
            </router-link>
          </template>
        </EmptyState>
      </div>

      <div v-else class="grid grid-cols-1 gap-4 p-5 md:grid-cols-2 xl:grid-cols-3">
        <div
          v-for="card in projectCards"
          :key="card.project.id"
          class="flex flex-col justify-between rounded-xl border border-slate-200/90 bg-white p-4 shadow-sm transition hover:border-indigo-300/80 hover:shadow"
          data-testid="overview-project-card"
        >
          <!-- Card Header: Name, Domain/Slug, Latest Run Status -->
          <div>
            <div class="flex items-start justify-between gap-2">
              <div class="min-w-0">
                <router-link
                  :to="{ name: 'projects', query: { projectId: card.project.id } }"
                  class="block truncate text-sm font-bold text-slate-900 hover:text-indigo-700 hover:underline"
                >
                  {{ card.project.name }}
                </router-link>
                <div class="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-500">
                  <span
                    v-if="card.primaryDomain"
                    class="inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-600"
                  >
                    <Globe class="h-3 w-3 text-slate-400" />
                    <span class="truncate">{{ card.primaryDomain }}</span>
                  </span>
                  <span v-else class="font-mono text-slate-400">{{ card.project.slug }}</span>
                </div>
              </div>

              <StatusBadge v-if="card.latestRun" :status="card.latestRun.status" />
              <span
                v-else
                class="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-500"
              >
                {{ t("overview.projects.noRuns") }}
              </span>
            </div>

            <p class="mt-2 line-clamp-2 text-xs leading-relaxed text-slate-500">
              {{ card.project.description || t("overview.projects.noDesc") }}
            </p>

            <p v-if="card.latestRun" class="mt-1.5 text-[11px] text-slate-400">
              {{
                t("overview.projects.latestRunTime", {
                  time: formatDate(card.latestRun.started_at),
                })
              }}
            </p>

            <!-- Isolated Project Latest-Run Metrics Strip -->
            <div class="mt-3.5 grid grid-cols-4 gap-2 rounded-lg border border-slate-100 bg-slate-50/80 p-2.5 text-center">
              <div>
                <p class="text-[10px] font-medium text-slate-500">
                  {{ t("overview.projects.metric.endpoints") }}
                </p>
                <p class="mt-0.5 font-mono text-sm font-semibold text-slate-900 tabular-nums">
                  {{ card.endpoints ?? "—" }}
                </p>
              </div>
              <div>
                <p class="text-[10px] font-medium text-slate-500">
                  {{ t("overview.projects.metric.coverage") }}
                </p>
                <p class="mt-0.5 font-mono text-sm font-semibold text-emerald-700 tabular-nums">
                  {{ card.coveragePct !== null ? `${card.coveragePct.toFixed(0)}%` : "—" }}
                </p>
              </div>
              <div>
                <p class="text-[10px] font-medium text-slate-500">
                  {{ t("overview.projects.metric.highCrit") }}
                </p>
                <p
                  class="mt-0.5 font-mono text-sm font-semibold tabular-nums"
                  :class="
                    card.highCritical === null
                      ? 'text-slate-400'
                      : card.highCritical > 0
                        ? 'text-rose-600'
                        : 'text-emerald-700'
                  "
                >
                  {{ card.highCritical ?? "—" }}
                </p>
              </div>
              <div>
                <p class="text-[10px] font-medium text-slate-500">
                  {{ t("overview.projects.metric.runs") }}
                </p>
                <p class="mt-0.5 font-mono text-sm font-semibold text-indigo-700 tabular-nums">
                  {{ card.runCount }}
                </p>
              </div>
            </div>
          </div>

          <!-- Card Actions -->
          <div class="mt-4 flex flex-wrap items-center gap-1.5 border-t border-slate-100 pt-3">
            <router-link
              v-if="card.latestRun"
              :to="`/runs/${card.latestRun.id}`"
              class="btn-primary !px-2.5 !py-1.5 !text-xs"
            >
              <span>{{ t("overview.projects.action.latestRun") }}</span>
              <ArrowRight class="h-3 w-3" />
            </router-link>
            <router-link
              :to="{ name: 'runs', query: { new: '1', projectId: card.project.id } }"
              class="btn-secondary !px-2.5 !py-1.5 !text-xs"
            >
              <PlayCircle class="h-3.5 w-3.5 text-indigo-600" />
              <span>{{ t("overview.projects.action.newRun") }}</span>
            </router-link>
            <router-link
              :to="{ name: 'projects', query: { projectId: card.project.id } }"
              class="btn-secondary !px-2.5 !py-1.5 !text-xs"
            >
              <BookOpen class="h-3.5 w-3.5 text-slate-500" />
              <span>{{ t("overview.projects.action.config") }}</span>
            </router-link>
          </div>
        </div>
      </div>
    </section>

    <!-- Recent Runs Table -->
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
            @click="loadOverview"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
            <span>{{ t("common.refresh") }}</span>
          </button>
          <router-link to="/runs" class="btn-secondary !px-2.5 !py-1.5 !text-xs">
            <span>{{ t("overview.recent.allRuns") }}</span>
            <ArrowRight class="h-3.5 w-3.5" />
          </router-link>
        </div>
      </div>

      <div v-if="loading" class="p-6 text-center text-sm text-slate-500">
        {{ t("overview.recent.loading") }}
      </div>

      <div v-else-if="recentRuns.length === 0" class="p-6">
        <EmptyState
          :title="t('overview.recent.emptyTitle')"
          :description="t('overview.recent.emptyDesc')"
        >
          <template #action>
            <router-link to="/projects" class="btn-secondary">
              {{ t("overview.recent.createProject") }}
            </router-link>
            <router-link :to="{ name: 'runs', query: { new: '1' } }" class="btn-primary">
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
              <th class="px-4 py-3">{{ t("overview.table.project") }}</th>
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
              v-for="run in recentRuns.slice(0, 8)"
              :key="run.id"
              class="transition hover:bg-slate-50/80"
            >
              <td class="px-5 py-3">
                <router-link
                  :to="`/runs/${run.id}`"
                  class="block max-w-[240px] truncate text-xs font-semibold text-slate-900 hover:text-indigo-700 hover:underline"
                  :title="getRunTitle(run, run.projectName)"
                >
                  {{ getRunTitle(run, run.projectName) }}
                </router-link>
                <span class="font-mono text-[10px] text-slate-400" :title="run.id">
                  {{ shortRunId(run.id) }}
                </span>
              </td>
              <td class="whitespace-nowrap px-4 py-3 text-xs font-medium text-slate-800">
                {{ run.projectName }}
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
                    :to="`/runs/${run.id}`"
                    class="text-indigo-600 hover:text-indigo-800 hover:underline"
                  >
                    {{ t("common.open") }}
                  </router-link>
                  <span class="text-slate-300">·</span>
                  <router-link
                    :to="{ path: `/runs/${run.id}`, query: { tab: 'java' } }"
                    class="text-slate-600 hover:text-slate-900 hover:underline"
                  >
                    {{ t("overview.link.api") }}
                  </router-link>
                  <span class="text-slate-300">·</span>
                  <router-link
                    :to="{ path: `/runs/${run.id}`, query: { tab: 'performance' } }"
                    class="text-slate-600 hover:text-slate-900 hover:underline"
                  >
                    {{ t("overview.link.perf") }}
                  </router-link>
                  <span class="text-slate-300">·</span>
                  <router-link
                    :to="{ path: `/runs/${run.id}`, query: { tab: 'security' } }"
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

    <!-- Environment Toolchain Doctor Grid: Core Execution vs Optional Discovery/Capture -->
    <section class="card overflow-hidden">
      <div class="card-header">
        <div class="flex items-center gap-2.5">
          <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
            <Wrench class="h-4 w-4" />
          </div>
          <div>
            <h3 class="text-sm font-semibold text-slate-900">{{ t("overview.doctor.title") }}</h3>
            <p class="text-xs text-slate-500">
              {{ t("overview.doctor.subtitle") }}
              <code class="font-mono text-[11px]">GET /api/v1/system/doctor</code>
            </p>
          </div>
        </div>
        <div class="flex items-center gap-2.5">
          <span
            v-if="!doctorLoading && doctorEntries.length > 0"
            class="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700"
          >
            {{
              t("overview.doctor.summary", {
                coreReady: coreAvailableCount,
                coreTotal: coreExecutionTools.length,
                totalReady: doctorAvailableCount,
                total: doctorEntries.length,
              })
            }}
          </span>
          <button
            type="button"
            class="btn-secondary !px-2.5 !py-1.5 !text-xs"
            :disabled="doctorLoading"
            data-testid="doctor-recheck-btn"
            @click="loadDoctor"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="doctorLoading ? 'animate-spin' : ''" />
            <span>{{ t("overview.doctor.recheck") }}</span>
          </button>
        </div>
      </div>

      <!-- Reassuring out-of-the-box banner -->
      <div
        class="flex items-start gap-2.5 border-b border-slate-100 bg-indigo-50/45 px-5 py-3 text-xs text-indigo-950"
      >
        <Info class="mt-0.5 h-4 w-4 shrink-0 text-indigo-600" />
        <span class="leading-relaxed">{{ t("overview.doctor.reassureBanner") }}</span>
      </div>

      <div v-if="doctorLoading" class="p-6 text-center text-sm text-slate-500">
        {{ t("overview.doctor.loading") }}
      </div>

      <div v-else-if="doctorError" class="p-5">
        <div class="flex items-center gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-800">
          <AlertTriangle class="h-4 w-4 shrink-0 text-amber-600" />
          <span>{{ doctorError }}</span>
        </div>
      </div>

      <div
        v-else-if="doctorEntries.length === 0"
        class="p-6 text-center text-xs text-slate-500"
      >
        {{ t("overview.doctor.empty") }}
      </div>

      <div v-else class="space-y-6 p-5">
        <!-- Group 1: Core Execution -->
        <div>
          <div class="mb-3 flex items-center justify-between">
            <div>
              <h4 class="text-xs font-semibold uppercase tracking-wider text-slate-700">
                {{ t("overview.doctor.coreTitle") }}
              </h4>
              <p class="text-[11px] text-slate-500">
                {{ t("overview.doctor.coreDesc") }}
              </p>
            </div>
            <span class="rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700">
              {{
                t("overview.doctor.coreReady", {
                  ready: coreAvailableCount,
                  total: coreExecutionTools.length,
                })
              }}
            </span>
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            <button
              v-for="tool in coreExecutionTools"
              :key="tool.name"
              type="button"
              class="group flex flex-col justify-between rounded-xl border p-3.5 text-left transition hover:border-indigo-300 hover:shadow-sm focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
              :class="
                tool.available
                  ? 'border-emerald-200/80 bg-emerald-50/25 hover:bg-emerald-50/45'
                  : 'border-slate-200 bg-slate-50/60 hover:bg-white'
              "
              :data-testid="`doctor-tool-${tool.name}`"
              @click="openToolModal(tool.name)"
            >
              <div class="flex w-full items-start justify-between gap-2">
                <div class="min-w-0">
                  <div class="flex items-center gap-1.5">
                    <CheckCircle2
                      v-if="tool.available"
                      class="h-4 w-4 shrink-0 text-emerald-600"
                    />
                    <XCircle v-else class="h-4 w-4 shrink-0 text-slate-400" />
                    <span class="truncate font-mono text-sm font-semibold text-slate-900">
                      {{ tool.name }}
                    </span>
                  </div>
                  <div class="mt-1 flex flex-wrap items-center gap-1">
                    <span
                      class="inline-block rounded bg-white/90 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-500 ring-1 ring-inset ring-slate-200"
                    >
                      {{ tool.category }}
                    </span>
                    <span
                      v-if="!tool.available && tool.auto_installable"
                      class="inline-flex items-center gap-0.5 rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700 ring-1 ring-inset ring-indigo-600/20"
                    >
                      <Download class="h-2.5 w-2.5" />
                      <span>{{ t("overview.doctor.autoBadge") }}</span>
                    </span>
                  </div>
                </div>
                <StatusBadge :status="tool.available ? 'available' : 'missing'" />
              </div>

              <div
                class="mt-3 flex w-full items-center justify-between gap-2 border-t border-slate-200/70 pt-2 text-[11px]"
              >
                <div
                  v-if="tool.available && tool.path"
                  class="flex min-w-0 items-center gap-1.5 truncate font-mono text-slate-600"
                  :title="tool.path"
                >
                  <Terminal class="h-3 w-3 shrink-0 text-slate-400" />
                  <span class="truncate">{{ tool.path }}</span>
                </div>
                <div v-else class="min-w-0 truncate text-slate-500" :title="tool.install_hint">
                  {{ tool.install_hint }}
                </div>

                <span
                  class="shrink-0 font-medium transition"
                  :class="
                    tool.available
                      ? 'text-slate-400 group-hover:text-indigo-600'
                      : 'text-indigo-600 group-hover:text-indigo-800'
                  "
                >
                  {{
                    tool.available
                      ? t("overview.doctor.detailsAction")
                      : t("overview.doctor.setupAction")
                  }}
                  →
                </span>
              </div>
            </button>
          </div>
        </div>

        <!-- Group 2: Optional Discovery & Capture Helpers -->
        <div v-if="optionalDiscoveryTools.length > 0" class="border-t border-slate-100 pt-5">
          <div class="mb-3 flex items-center justify-between">
            <div>
              <h4 class="text-xs font-semibold uppercase tracking-wider text-slate-500">
                {{ t("overview.doctor.optionalTitle") }}
              </h4>
              <p class="text-[11px] text-slate-400">
                {{ t("overview.doctor.optionalDesc") }}
              </p>
            </div>
            <span class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
              {{ t("common.optional") }}
            </span>
          </div>

          <div class="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
            <button
              v-for="tool in optionalDiscoveryTools"
              :key="tool.name"
              type="button"
              class="group flex flex-col justify-between rounded-lg border border-slate-200/80 bg-slate-50/40 p-3 text-left transition hover:border-indigo-300 hover:bg-white hover:shadow-sm focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
              :data-testid="`doctor-tool-${tool.name}`"
              @click="openToolModal(tool.name)"
            >
              <div class="flex w-full items-center justify-between gap-2">
                <div class="flex min-w-0 items-center gap-1.5">
                  <CheckCircle2
                    v-if="tool.available"
                    class="h-3.5 w-3.5 shrink-0 text-emerald-600"
                  />
                  <XCircle v-else class="h-3.5 w-3.5 shrink-0 text-slate-400" />
                  <span class="truncate font-mono text-xs font-semibold text-slate-800">
                    {{ tool.name }}
                  </span>
                </div>
                <StatusBadge :status="tool.available ? 'available' : 'missing'" />
              </div>

              <div class="mt-2 flex w-full items-center justify-between gap-2 text-[10px]">
                <p
                  class="min-w-0 truncate font-mono text-slate-500"
                  :title="tool.available && tool.path ? tool.path : tool.install_hint"
                >
                  {{ tool.available && tool.path ? tool.path : tool.install_hint }}
                </p>
                <span
                  class="shrink-0 font-medium"
                  :class="
                    tool.available
                      ? 'text-slate-400 group-hover:text-indigo-600'
                      : 'text-indigo-600 group-hover:text-indigo-800'
                  "
                >
                  {{
                    tool.available
                      ? t("overview.doctor.detailsAction")
                      : t("overview.doctor.setupAction")
                  }}
                </span>
              </div>
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- Tool Setup Modal -->
    <div
      v-if="selectedTool"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
      data-testid="doctor-setup-modal"
      @click.self="closeToolModal"
    >
      <div class="w-full max-w-xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
        <!-- Modal Header -->
        <div class="flex items-center justify-between gap-3 border-b border-slate-100 px-6 py-4">
          <div class="flex min-w-0 items-center gap-2.5">
            <div
              class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg"
              :class="
                selectedTool.available
                  ? 'bg-emerald-50 text-emerald-600'
                  : 'bg-indigo-50 text-indigo-600'
              "
            >
              <Wrench class="h-4 w-4" />
            </div>
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="font-mono text-base font-bold text-slate-900">
                  {{ selectedTool.name }}
                </h3>
                <span
                  class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-600"
                >
                  {{ selectedTool.category }}
                </span>
                <StatusBadge :status="selectedTool.available ? 'available' : 'missing'" />
              </div>
            </div>
          </div>

          <button
            type="button"
            class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 disabled:opacity-40"
            :disabled="installingTool"
            :aria-label="t('overview.doctor.modal.close')"
            @click="closeToolModal"
          >
            <X class="h-4 w-4" />
          </button>
        </div>

        <!-- Modal Body -->
        <div class="max-h-[75vh] space-y-4 overflow-y-auto px-6 py-5 text-sm">
          <!-- Resolved Path when available -->
          <div
            v-if="selectedTool.available && selectedTool.path"
            class="rounded-xl border border-emerald-200/80 bg-emerald-50/40 p-3.5"
          >
            <p class="text-[11px] font-semibold uppercase tracking-wider text-emerald-800">
              {{ t("overview.doctor.modal.path") }}
            </p>
            <p class="mt-1 break-all font-mono text-xs text-emerald-950">
              {{ selectedTool.path }}
            </p>
          </div>

          <!-- Purpose & Impact -->
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div class="rounded-xl border border-slate-200/80 bg-slate-50/70 p-3.5">
              <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                {{ t("overview.doctor.modal.purpose") }}
              </p>
              <p class="mt-1.5 text-xs leading-relaxed text-slate-800">
                {{ selectedToolPurpose }}
              </p>
            </div>

            <div class="rounded-xl border border-slate-200/80 bg-slate-50/70 p-3.5">
              <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                {{ t("overview.doctor.modal.impact") }}
              </p>
              <p class="mt-1.5 text-xs leading-relaxed text-slate-700">
                {{ selectedToolImpact }}
              </p>
            </div>
          </div>

          <!-- Install Command -->
          <div v-if="selectedToolInstallCmd">
            <div class="mb-1.5 flex items-center justify-between">
              <span class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
                <Terminal class="h-3.5 w-3.5 text-slate-500" />
                <span>{{ t("overview.doctor.modal.installCmd") }}</span>
              </span>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 transition hover:bg-slate-50 hover:text-slate-900"
                data-testid="doctor-copy-install-cmd"
                @click="copyCommand(selectedToolInstallCmd, 'install')"
              >
                <Check v-if="copiedField === 'install'" class="h-3 w-3 text-emerald-600" />
                <Copy v-else class="h-3 w-3" />
                <span>
                  {{
                    copiedField === "install"
                      ? t("overview.doctor.modal.copied")
                      : t("overview.doctor.modal.copy")
                  }}
                </span>
              </button>
            </div>
            <pre
              class="overflow-x-auto rounded-xl bg-slate-900 px-3.5 py-2.5 font-mono text-xs text-slate-100"
            ><code>{{ selectedToolInstallCmd }}</code></pre>
          </div>

          <!-- Docker Alternative -->
          <div v-if="selectedTool.docker_alt">
            <div class="mb-1.5 flex items-center justify-between">
              <span class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
                <Container class="h-3.5 w-3.5 text-sky-600" />
                <span>{{ t("overview.doctor.modal.dockerAlt") }}</span>
              </span>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 transition hover:bg-slate-50 hover:text-slate-900"
                @click="copyCommand(selectedTool.docker_alt, 'docker')"
              >
                <Check v-if="copiedField === 'docker'" class="h-3 w-3 text-emerald-600" />
                <Copy v-else class="h-3 w-3" />
                <span>
                  {{
                    copiedField === "docker"
                      ? t("overview.doctor.modal.copied")
                      : t("overview.doctor.modal.copy")
                  }}
                </span>
              </button>
            </div>
            <pre
              class="overflow-x-auto rounded-xl bg-slate-900 px-3.5 py-2.5 font-mono text-xs text-sky-200"
            ><code>{{ selectedTool.docker_alt }}</code></pre>
          </div>

          <!-- Manual install guidance when not auto-installable -->
          <div
            v-if="!selectedTool.available && !selectedTool.auto_installable"
            class="rounded-xl border border-amber-200/80 bg-amber-50/70 px-3.5 py-2.5 text-xs leading-relaxed text-amber-900"
          >
            {{ t("overview.doctor.modal.manualHint") }}
          </div>

          <!-- Install error -->
          <div
            v-if="installError"
            class="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800"
          >
            <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
            <span>{{ installError }}</span>
          </div>

          <!-- Installer Result & Output -->
          <div v-if="installResult" class="space-y-2">
            <div
              class="flex items-center gap-2 rounded-xl border px-3.5 py-2.5 text-xs font-medium"
              :class="
                installResult.ok
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                  : 'border-rose-200 bg-rose-50 text-rose-800'
              "
            >
              <CheckCircle2 v-if="installResult.ok" class="h-4 w-4 shrink-0 text-emerald-600" />
              <AlertTriangle v-else class="h-4 w-4 shrink-0 text-rose-600" />
              <span>
                {{
                  installResult.ok
                    ? t("overview.doctor.modal.installOk")
                    : t("overview.doctor.modal.installFail", { code: installResult.returncode })
                }}
              </span>
            </div>

            <div v-if="installResult.output">
              <p class="mb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                {{ t("overview.doctor.modal.outputTitle") }}
              </p>
              <pre
                class="max-h-44 overflow-auto rounded-xl bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200"
                data-testid="doctor-install-output"
              >{{ installResult.output }}</pre>
            </div>
          </div>
        </div>

        <!-- Modal Footer -->
        <div class="flex items-center justify-between gap-2 border-t border-slate-100 bg-slate-50/70 px-6 py-3.5">
          <button
            type="button"
            class="btn-secondary !py-1.5 !text-xs"
            :disabled="doctorLoading || installingTool"
            @click="loadDoctor"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="doctorLoading ? 'animate-spin' : ''" />
            <span>{{ t("overview.doctor.recheck") }}</span>
          </button>

          <div class="flex items-center gap-2">
            <button
              v-if="!selectedTool.available && selectedTool.auto_installable"
              type="button"
              class="btn-primary !py-1.5 !text-xs"
              :disabled="installingTool"
              data-testid="doctor-auto-install-btn"
              @click="handleAutoInstall"
            >
              <Loader2 v-if="installingTool" class="h-3.5 w-3.5 animate-spin" />
              <Download v-else class="h-3.5 w-3.5" />
              <span>
                {{
                  installingTool
                    ? t("overview.doctor.modal.installing")
                    : t("overview.doctor.modal.autoInstall")
                }}
              </span>
            </button>
            <button
              type="button"
              class="btn-secondary !py-1.5 !text-xs"
              :disabled="installingTool"
              @click="closeToolModal"
            >
              {{ t("overview.doctor.modal.close") }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
