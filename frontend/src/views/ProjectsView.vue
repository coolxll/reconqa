<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Database,
  FolderKanban,
  Globe,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Trash2,
  X,
} from "lucide-vue-next";
import { listTargets } from "@/api/projects";
import { listProjectRuns } from "@/api/runs";
import type { Project, Run, Target } from "@/api/types";
import { useWorkspace } from "@/stores/workspace";
import { useI18n } from "@/i18n";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import EmptyState from "@/components/layout/EmptyState.vue";

interface ProjectDirectoryCard {
  project: Project;
  primaryDomain: string | null;
  latestRun: Run | null;
  runCount: number;
  endpoints: number | null;
  coveragePct: number | null;
  highCritical: number | null;
}

const router = useRouter();
const { locale, t } = useI18n();
const {
  projects,
  activeProjectId,
  loadingProjects,
  loadProjects,
  createProject: createWorkspaceProject,
  deleteProject: deleteWorkspaceProject,
  setActiveProject,
} = useWorkspace();

const projectSearch = ref("");
const loadingStats = ref(false);
const runsByProject = ref<Record<string, Run[]>>({});
const targetsByProject = ref<Record<string, Target[]>>({});
const bannerError = ref<string | null>(null);
const bannerSuccess = ref<string | null>(null);

// Create Project Modal state
const showCreateModal = ref(false);
const creatingProject = ref(false);
const newProjectName = ref("");
const newProjectSlug = ref("");
const newProjectDesc = ref("");

// Delete confirmation state
const confirmDeleteProjectId = ref<string | null>(null);
const deletingId = ref<string | null>(null);

function notifySuccess(msg: string): void {
  bannerError.value = null;
  bannerSuccess.value = msg;
  window.setTimeout(() => {
    if (bannerSuccess.value === msg) {
      bannerSuccess.value = null;
    }
  }, 4000);
}

function notifyError(err: unknown, fallback: string): void {
  bannerSuccess.value = null;
  bannerError.value = err instanceof Error ? err.message : fallback;
}

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

const projectCards = computed<ProjectDirectoryCard[]>(() =>
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

const filteredCards = computed(() => {
  const q = projectSearch.value.trim().toLowerCase();
  if (!q) return projectCards.value;
  return projectCards.value.filter(
    (card) =>
      card.project.name.toLowerCase().includes(q) ||
      card.project.slug.toLowerCase().includes(q) ||
      card.project.description.toLowerCase().includes(q) ||
      (card.primaryDomain ?? "").toLowerCase().includes(q)
  );
});

async function refreshDirectory(): Promise<void> {
  loadingStats.value = true;
  bannerError.value = null;
  try {
    const list = await loadProjects();
    const [runLists, targetLists] = await Promise.all([
      Promise.all(list.map((p) => listProjectRuns(p.id).catch(() => [] as Run[]))),
      Promise.all(list.map((p) => listTargets(p.id).catch(() => [] as Target[]))),
    ]);

    const nextRuns: Record<string, Run[]> = {};
    const nextTargets: Record<string, Target[]> = {};
    list.forEach((p, idx) => {
      nextRuns[p.id] = [...(runLists[idx] ?? [])].sort((a, b) => {
        const ta = a.started_at ? new Date(a.started_at).getTime() : 0;
        const tb = b.started_at ? new Date(b.started_at).getTime() : 0;
        return tb - ta;
      });
      nextTargets[p.id] = targetLists[idx] ?? [];
    });

    runsByProject.value = nextRuns;
    targetsByProject.value = nextTargets;
  } catch (err) {
    notifyError(err, t("projects.err.loadProjects"));
  } finally {
    loadingStats.value = false;
  }
}

function enterWorkspace(projectId: string, section = "overview"): void {
  setActiveProject(projectId);
  void router.push(`/p/${projectId}/${section}`);
}

async function handleCreateProject(): Promise<void> {
  if (!newProjectName.value.trim()) return;
  creatingProject.value = true;
  try {
    const created = await createWorkspaceProject(
      {
        name: newProjectName.value.trim(),
        slug: newProjectSlug.value.trim() || null,
        description: newProjectDesc.value.trim(),
      },
      router
    );
    showCreateModal.value = false;
    newProjectName.value = "";
    newProjectSlug.value = "";
    newProjectDesc.value = "";
    notifySuccess(t("projects.toast.projectCreated", { name: created.name }));
  } catch (err) {
    notifyError(err, t("projects.err.createProject"));
  } finally {
    creatingProject.value = false;
  }
}

async function handleDeleteProject(projectId: string): Promise<void> {
  if (deletingId.value) return;
  deletingId.value = projectId;
  try {
    await deleteWorkspaceProject(projectId);
    confirmDeleteProjectId.value = null;
    notifySuccess(t("projects.toast.projectDeleted"));
  } catch (err) {
    notifyError(err, t("projects.err.deleteProject"));
  } finally {
    deletingId.value = null;
  }
}

onMounted(() => {
  void refreshDirectory();
});
</script>

<template>
  <div class="space-y-5">
    <!-- Alerts -->
    <div
      v-if="bannerError"
      class="flex items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
    >
      <div class="flex items-center gap-2">
        <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
        <span>{{ bannerError }}</span>
      </div>
      <button type="button" class="text-rose-500 hover:text-rose-700" @click="bannerError = null">
        <X class="h-4 w-4" />
      </button>
    </div>

    <div
      v-if="bannerSuccess"
      class="flex items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800"
    >
      <div class="flex items-center gap-2">
        <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-600" />
        <span>{{ bannerSuccess }}</span>
      </div>
      <button
        type="button"
        class="text-emerald-500 hover:text-emerald-700"
        @click="bannerSuccess = null"
      >
        <X class="h-4 w-4" />
      </button>
    </div>

    <!-- Header & Search Bar -->
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <FolderKanban class="h-5 w-5 text-indigo-600" />
          <h1 class="text-xl font-bold tracking-tight text-slate-900">
            {{ t("workspaces.title") }}
          </h1>
          <span class="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-semibold text-slate-600">
            {{ projects.length }}
          </span>
        </div>
        <p class="mt-1 text-xs text-slate-500">
          {{ t("workspaces.subtitle") }}
        </p>
      </div>

      <div class="flex flex-wrap items-center gap-2.5">
        <div class="relative w-64">
          <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input
            v-model="projectSearch"
            type="text"
            :placeholder="t('workspaces.searchPlaceholder')"
            class="input-field !py-1.5 !pl-9 text-xs"
            data-testid="workspaces-search-input"
          />
        </div>
        <button
          type="button"
          class="btn-secondary !py-1.5 !text-xs"
          :disabled="loadingProjects || loadingStats"
          @click="refreshDirectory"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="loadingProjects || loadingStats ? 'animate-spin' : ''" />
          <span>{{ t("common.refresh") }}</span>
        </button>
        <button
          type="button"
          class="btn-primary !py-1.5 !text-xs"
          data-testid="workspaces-new-project-btn"
          @click="showCreateModal = true"
        >
          <Plus class="h-3.5 w-3.5" />
          <span>{{ t("workspaces.newProjectBtn") }}</span>
        </button>
      </div>
    </div>

    <!-- Loading -->
    <div
      v-if="loadingProjects && projects.length === 0"
      class="card p-10 text-center text-sm text-slate-500"
    >
      {{ t("projects.loading") }}
    </div>

    <!-- Empty Directory -->
    <div v-else-if="filteredCards.length === 0" class="card p-8">
      <EmptyState
        :title="t('workspaces.emptyTitle')"
        :description="t('workspaces.emptyDesc')"
      >
        <template #icon>
          <FolderKanban class="h-5 w-5 text-indigo-500" />
        </template>
        <template #action>
          <button type="button" class="btn-primary" @click="showCreateModal = true">
            <Plus class="h-4 w-4" />
            <span>{{ t("workspaces.newProjectBtn") }}</span>
          </button>
        </template>
      </EmptyState>
    </div>

    <!-- Project Workspace Card Grid -->
    <div
      v-else
      class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
      data-testid="workspaces-card-grid"
    >
      <div
        v-for="card in filteredCards"
        :key="card.project.id"
        class="flex flex-col justify-between rounded-xl border bg-white p-5 shadow-sm transition hover:border-indigo-300 hover:shadow"
        :class="
          card.project.id === activeProjectId
            ? 'border-indigo-400 ring-1 ring-indigo-500/20'
            : 'border-slate-200/90'
        "
        data-testid="workspace-project-card"
      >
        <!-- Header -->
        <div>
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <button
                type="button"
                class="block max-w-full truncate text-left text-base font-bold text-slate-900 hover:text-indigo-700 hover:underline"
                @click="enterWorkspace(card.project.id, 'overview')"
              >
                {{ card.project.name }}
              </button>
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

            <div class="flex shrink-0 items-center gap-1.5">
              <StatusBadge v-if="card.latestRun" :status="card.latestRun.status" />
              <span
                v-else
                class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-500"
              >
                {{ t("overview.projects.noRuns") }}
              </span>
            </div>
          </div>

          <p class="mt-2.5 line-clamp-2 text-xs leading-relaxed text-slate-500">
            {{ card.project.description || t("overview.projects.noDesc") }}
          </p>

          <p v-if="card.latestRun" class="mt-1.5 text-[11px] text-slate-400">
            {{
              t("overview.projects.latestRunTime", {
                time: formatDate(card.latestRun.started_at),
              })
            }}
          </p>

          <!-- Isolated Project Metrics Strip -->
          <div
            class="mt-4 grid grid-cols-4 gap-2 rounded-lg border border-slate-100 bg-slate-50/80 p-2.5 text-center"
          >
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
        <div class="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3">
          <div class="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              class="btn-primary !px-3 !py-1.5 !text-xs"
              data-testid="enter-workspace-btn"
              @click="enterWorkspace(card.project.id, 'overview')"
            >
              <span>{{ t("workspaces.enterBtn") }}</span>
              <ArrowRight class="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              class="btn-secondary !px-2 !py-1.5 !text-xs"
              :title="t('workspaces.inventoryLink')"
              @click="enterWorkspace(card.project.id, 'inventory')"
            >
              <Database class="h-3.5 w-3.5 text-indigo-600" />
              <span class="hidden sm:inline">{{ t("workspaces.inventoryLink") }}</span>
            </button>
            <button
              type="button"
              class="btn-secondary !px-2 !py-1.5 !text-xs"
              :title="t('workspaces.kbLink')"
              @click="enterWorkspace(card.project.id, 'kb')"
            >
              <BookOpen class="h-3.5 w-3.5 text-slate-500" />
            </button>
            <button
              type="button"
              class="btn-secondary !px-2 !py-1.5 !text-xs"
              :title="t('workspaces.settingsLink')"
              @click="enterWorkspace(card.project.id, 'settings')"
            >
              <Settings class="h-3.5 w-3.5 text-slate-500" />
            </button>
          </div>

          <div class="flex items-center gap-1">
            <template v-if="confirmDeleteProjectId === card.project.id">
              <button
                type="button"
                class="rounded bg-rose-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-rose-700 disabled:opacity-50"
                :disabled="deletingId === card.project.id"
                @click="handleDeleteProject(card.project.id)"
              >
                {{ t("common.confirm") }}
              </button>
              <button
                type="button"
                class="rounded bg-slate-200 px-1.5 py-1 text-[11px] text-slate-700 hover:bg-slate-300"
                @click="confirmDeleteProjectId = null"
              >
                {{ t("common.cancel") }}
              </button>
            </template>
            <button
              v-else
              type="button"
              class="rounded p-1.5 text-slate-400 transition hover:bg-rose-50 hover:text-rose-600"
              :title="t('projects.deleteTitle')"
              @click="confirmDeleteProjectId = card.project.id"
            >
              <Trash2 class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Create Project Modal -->
    <Teleport to="body">
      <div
        v-if="showCreateModal"
        class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4 backdrop-blur-sm"
        @click.self="showCreateModal = false"
      >
        <div class="w-full max-w-md overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
          <div class="flex items-center justify-between border-b border-slate-200 px-5 py-4">
            <h3 class="text-sm font-semibold text-slate-900">
              {{ t("projects.modal.title") }}
            </h3>
            <button
              type="button"
              class="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              @click="showCreateModal = false"
            >
              <X class="h-4 w-4" />
            </button>
          </div>

          <form class="space-y-4 p-5" @submit.prevent="handleCreateProject">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.modal.nameLabel") }}
              </label>
              <input
                v-model="newProjectName"
                type="text"
                required
                :placeholder="t('projects.modal.namePlaceholder')"
                class="input-field"
              />
            </div>

            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.modal.slugLabel") }}
                <span class="text-slate-400">{{ t("projects.modal.slugHint") }}</span>
              </label>
              <input
                v-model="newProjectSlug"
                type="text"
                placeholder="example-storefront"
                class="input-field font-mono text-xs"
              />
            </div>

            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.modal.descLabel") }}
              </label>
              <textarea
                v-model="newProjectDesc"
                rows="3"
                :placeholder="t('projects.modal.descPlaceholder')"
                class="input-field"
              />
            </div>

            <div class="flex justify-end gap-2 pt-2">
              <button
                type="button"
                class="btn-secondary"
                @click="showCreateModal = false"
              >
                {{ t("common.cancel") }}
              </button>
              <button
                type="submit"
                class="btn-primary"
                :disabled="creatingProject || !newProjectName.trim()"
              >
                {{ creatingProject ? t("projects.modal.creating") : t("projects.modal.submit") }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>
  </div>
</template>
