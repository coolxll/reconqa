<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Activity,
  BookOpen,
  Boxes,
  Check,
  ChevronsUpDown,
  Compass,
  FolderKanban,
  LayoutDashboard,
  Layers,
  ListChecks,
  PanelLeftClose,
  PanelLeftOpen,
  PlayCircle,
  Plus,
  Radar,
  Search,
  Settings,
  TrendingUp,
  Wrench,
  X,
} from "lucide-vue-next";
import { listTargets } from "@/api/projects";
import type { Target } from "@/api/types";
import { useI18n } from "@/i18n";
import { useWorkspace, type WorkspaceSection } from "@/stores/workspace";

const props = withDefaults(
  defineProps<{
    collapsed?: boolean;
  }>(),
  {
    collapsed: false,
  }
);

const emit = defineEmits<{
  toggle: [];
}>();

const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const {
  projects,
  activeProjectId,
  activeProject,
  ensureLoaded,
  selectProject,
  createProject,
  workspacePath,
} = useWorkspace();

const apiOnline = ref<boolean | null>(null);
const switcherOpen = ref(false);
const switcherSearch = ref("");
const switcherRef = ref<HTMLElement | null>(null);
const activeProjectTargets = ref<Target[]>([]);

async function refreshActiveTargets(pid: string): Promise<void> {
  if (!pid) {
    activeProjectTargets.value = [];
    return;
  }
  try {
    activeProjectTargets.value = await listTargets(pid);
  } catch {
    activeProjectTargets.value = [];
  }
}

watch(
  activeProjectId,
  (pid) => {
    void refreshActiveTargets(pid);
  },
  { immediate: true }
);

const activeProjectSubtitle = computed(() => {
  const count = activeProjectTargets.value.length;
  if (count > 1) {
    return t("projOverview.portalsCount", { count });
  }
  if (count === 1) {
    return activeProjectTargets.value[0].root_domain || "";
  }
  return "";
});

// Quick Create Project modal
const createModalOpen = ref(false);
const creating = ref(false);
const createError = ref("");
const newName = ref("");
const newSlug = ref("");
const newDesc = ref("");

const workspaceNavItems = computed(() => [
  {
    section: "overview" as WorkspaceSection,
    to: workspacePath("overview"),
    label: t("ws.nav.overview"),
    hint: t("ws.nav.overviewHint"),
    icon: LayoutDashboard,
  },
  {
    section: "explore" as WorkspaceSection,
    to: workspacePath("explore"),
    label: t("ws.nav.explore"),
    hint: t("ws.nav.exploreHint"),
    icon: Compass,
  },
  {
    section: "inventory" as WorkspaceSection,
    to: workspacePath("inventory"),
    label: t("ws.nav.inventory"),
    hint: t("ws.nav.inventoryHint"),
    icon: Layers,
  },
  {
    section: "capabilities" as WorkspaceSection,
    to: workspacePath("capabilities"),
    label: t("ws.nav.capabilities"),
    hint: t("ws.nav.capabilitiesHint"),
    icon: Boxes,
  },
  {
    section: "scenarios" as WorkspaceSection,
    to: workspacePath("scenarios"),
    label: t("ws.nav.scenarios"),
    hint: t("ws.nav.scenariosHint"),
    icon: ListChecks,
  },
  {
    section: "runs" as WorkspaceSection,
    to: workspacePath("runs"),
    label: t("ws.nav.runs"),
    hint: t("ws.nav.runsHint"),
    icon: PlayCircle,
  },
  {
    section: "kb" as WorkspaceSection,
    to: workspacePath("kb"),
    label: t("ws.nav.kb"),
    hint: t("ws.nav.kbHint"),
    icon: BookOpen,
  },
  {
    section: "trends" as WorkspaceSection,
    to: workspacePath("trends"),
    label: t("ws.nav.trends"),
    hint: t("ws.nav.trendsHint"),
    icon: TrendingUp,
  },
  {
    section: "settings" as WorkspaceSection,
    to: workspacePath("settings"),
    label: t("ws.nav.settings"),
    hint: t("ws.nav.settingsHint"),
    icon: Settings,
  },
]);

const activeSection = computed<string>(() => {
  if (route.meta?.workspaceSection) {
    return String(route.meta.workspaceSection);
  }
  if (route.name === "run-detail" || route.name === "project-run-detail") {
    return "runs";
  }
  if (route.name === "system-doctor") {
    return "doctor";
  }
  if (route.name === "projects") {
    return "projects";
  }
  return "";
});

const filteredProjects = computed(() => {
  const q = switcherSearch.value.trim().toLowerCase();
  if (!q) return projects.value;
  return projects.value.filter(
    (p) =>
      p.name.toLowerCase().includes(q) ||
      p.slug.toLowerCase().includes(q)
  );
});

const activeProjectInitial = computed(() => {
  const name = activeProject.value?.name?.trim();
  return name ? name.slice(0, 1).toUpperCase() : "P";
});

const apiStatusLabel = computed(() =>
  apiOnline.value === true
    ? t("nav.online")
    : apiOnline.value === false
      ? t("nav.offline")
      : t("nav.checking")
);

const toggleLabel = computed(() =>
  props.collapsed ? t("nav.expandSidebar") : t("nav.collapseSidebar")
);

async function handleSwitchProject(id: string): Promise<void> {
  switcherOpen.value = false;
  switcherSearch.value = "";
  await selectProject(id, router, route);
}

function openCreateModal(): void {
  switcherOpen.value = false;
  createError.value = "";
  newName.value = "";
  newSlug.value = "";
  newDesc.value = "";
  createModalOpen.value = true;
}

async function handleCreateProject(): Promise<void> {
  if (!newName.value.trim() || creating.value) return;
  creating.value = true;
  createError.value = "";
  try {
    await createProject(
      {
        name: newName.value.trim(),
        slug: newSlug.value.trim() || null,
        description: newDesc.value.trim(),
      },
      router,
      true
    );
    createModalOpen.value = false;
  } catch (err) {
    createError.value = err instanceof Error ? err.message : t("projects.err.createProject");
  } finally {
    creating.value = false;
  }
}

function onDocumentClick(event: MouseEvent): void {
  if (!switcherOpen.value) return;
  const el = switcherRef.value;
  if (el && !el.contains(event.target as Node)) {
    switcherOpen.value = false;
  }
}

onMounted(async () => {
  document.addEventListener("click", onDocumentClick);
  void ensureLoaded();
  try {
    const res = await fetch("/healthz");
    apiOnline.value = res.ok;
  } catch {
    apiOnline.value = false;
  }
});

onBeforeUnmount(() => {
  document.removeEventListener("click", onDocumentClick);
});
</script>

<template>
  <aside
    class="relative flex h-full shrink-0 flex-col border-r border-slate-800 bg-slate-900 text-slate-200 transition-[width] duration-200 ease-in-out"
    :class="collapsed ? 'w-[68px]' : 'w-64'"
    data-testid="sidebar-nav"
    :data-collapsed="collapsed ? 'true' : 'false'"
  >
    <!-- Brand Header -->
    <div
      class="flex h-14 shrink-0 items-center border-b border-slate-800/90"
      :class="collapsed ? 'justify-center px-3' : 'gap-2.5 px-4'"
    >
      <router-link
        :to="activeProjectId ? workspacePath('overview') : '/projects'"
        class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-indigo-600 text-white shadow-md shadow-indigo-950/40"
        :title="collapsed ? 'ReconQA' : undefined"
      >
        <Radar class="h-4 w-4" />
      </router-link>
      <div v-if="!collapsed" class="min-w-0 overflow-hidden whitespace-nowrap">
        <div class="flex items-center gap-1.5">
          <span class="text-sm font-bold tracking-tight text-white">ReconQA</span>
          <span
            class="rounded bg-indigo-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-300 ring-1 ring-inset ring-indigo-400/30"
          >
            v0.2
          </span>
        </div>
        <p class="truncate text-[10px] text-slate-400">{{ t("nav.brandTagline") }}</p>
      </div>
    </div>

    <!-- Jira-style Project / Workspace Switcher -->
    <div
      ref="switcherRef"
      class="relative shrink-0 border-b border-slate-800/90"
      :class="collapsed ? 'px-2 py-2.5' : 'px-3 py-2.5'"
    >
      <button
        type="button"
        data-testid="workspace-switcher"
        class="flex w-full items-center rounded-lg border border-slate-700/80 bg-slate-800/80 text-left transition hover:border-indigo-500/60 hover:bg-slate-800"
        :class="collapsed ? 'justify-center p-2' : 'justify-between gap-2 px-2.5 py-2'"
        :title="activeProject ? activeProject.name : t('ws.switcherLabel')"
        :aria-label="t('ws.switcherLabel')"
        :aria-expanded="switcherOpen"
        @click="switcherOpen = !switcherOpen"
      >
        <div class="flex min-w-0 items-center gap-2.5">
          <span
            class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-indigo-500/25 text-xs font-bold text-indigo-200 ring-1 ring-inset ring-indigo-400/30"
          >
            {{ activeProjectInitial }}
          </span>
          <div v-if="!collapsed" class="min-w-0">
            <div class="truncate text-xs font-semibold text-white">
              {{ activeProject?.name || t("ws.noProjectSelected") }}
            </div>
            <div v-if="activeProjectSubtitle" class="truncate font-mono text-[10px] text-slate-400">
              {{ activeProjectSubtitle }}
            </div>
          </div>
        </div>
        <ChevronsUpDown v-if="!collapsed" class="h-3.5 w-3.5 shrink-0 text-slate-400" />
      </button>

      <!-- Workspace Switcher Popover -->
      <div
        v-if="switcherOpen"
        data-testid="workspace-switcher-menu"
        class="absolute z-50 mt-1.5 w-64 overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl ring-1 ring-black/20"
        :class="collapsed ? 'left-16 top-1' : 'left-3 right-3'"
      >
        <div class="border-b border-slate-800 p-2">
          <div class="relative">
            <Search class="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-500" />
            <input
              v-model="switcherSearch"
              type="text"
              :placeholder="t('ws.searchProjects')"
              class="w-full rounded-lg border border-slate-700 bg-slate-800 py-1.5 pl-8 pr-2.5 text-xs text-slate-100 placeholder:text-slate-500 focus:border-indigo-500 focus:outline-none"
            />
          </div>
        </div>

        <div class="max-h-60 overflow-y-auto py-1">
          <button
            v-for="proj in filteredProjects"
            :key="proj.id"
            type="button"
            :data-testid="`workspace-option-${proj.id}`"
            class="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs transition hover:bg-slate-800"
            :class="proj.id === activeProjectId ? 'bg-indigo-600/20 text-white' : 'text-slate-300'"
            @click="handleSwitchProject(proj.id)"
          >
            <div class="min-w-0">
              <div class="truncate font-medium">{{ proj.name }}</div>
            </div>
            <Check
              v-if="proj.id === activeProjectId"
              class="h-3.5 w-3.5 shrink-0 text-indigo-400"
            />
          </button>
          <div
            v-if="filteredProjects.length === 0"
            class="px-3 py-4 text-center text-[11px] text-slate-500"
          >
            {{ t("projects.emptyTitle") }}
          </div>
        </div>

        <div class="space-y-1 border-t border-slate-800 bg-slate-950/60 p-2 text-xs">
          <button
            type="button"
            data-testid="workspace-new-project-btn"
            class="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 font-medium text-indigo-300 transition hover:bg-slate-800 hover:text-indigo-200"
            @click="openCreateModal"
          >
            <Plus class="h-3.5 w-3.5" />
            <span>{{ t("ws.newProject") }}</span>
          </button>
          <router-link
            to="/projects"
            data-testid="workspace-all-projects-link"
            class="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 font-medium text-slate-300 transition hover:bg-slate-800 hover:text-white"
            @click="switcherOpen = false"
          >
            <FolderKanban class="h-3.5 w-3.5 text-slate-400" />
            <span>{{ t("ws.allProjects") }}</span>
          </router-link>
        </div>
      </div>
    </div>

    <!-- Project-Scoped Navigation -->
    <nav
      class="flex-1 space-y-1 overflow-y-auto overflow-x-hidden py-3"
      :class="collapsed ? 'px-2' : 'px-3'"
    >
      <p
        v-if="!collapsed"
        class="whitespace-nowrap px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-widest text-slate-500"
      >
        {{ t("ws.sectionWorkspace") }}
      </p>

      <router-link
        v-for="item in workspaceNavItems"
        :key="item.section"
        :to="item.to"
        :data-testid="`nav-ws-${item.section}`"
        :title="collapsed ? item.label : undefined"
        :aria-label="item.label"
        class="group flex items-center rounded-lg text-sm transition"
        :class="[
          collapsed ? 'justify-center px-0 py-2' : 'gap-2.5 px-2.5 py-2',
          activeSection === item.section
            ? 'bg-indigo-600/90 text-white shadow-sm'
            : 'text-slate-300 hover:bg-slate-800 hover:text-white',
        ]"
      >
        <component
          :is="item.icon"
          class="h-4 w-4 shrink-0"
          :class="
            activeSection === item.section
              ? 'text-white'
              : 'text-slate-400 group-hover:text-slate-200'
          "
        />
        <div v-if="!collapsed" class="min-w-0 flex-1 overflow-hidden whitespace-nowrap">
          <div class="truncate text-[13px] font-medium leading-tight">{{ item.label }}</div>
          <div
            class="mt-0.5 truncate text-[10px] leading-tight"
            :class="activeSection === item.section ? 'text-indigo-100/80' : 'text-slate-500'"
          >
            {{ item.hint }}
          </div>
        </div>
      </router-link>
    </nav>

    <!-- Separated Platform System Section -->
    <div
      class="shrink-0 border-t border-slate-800 text-xs"
      :class="collapsed ? 'flex flex-col items-center gap-2 px-2 py-3' : 'space-y-2 px-3 py-3'"
    >
      <p
        v-if="!collapsed"
        class="whitespace-nowrap px-2.5 text-[10px] font-semibold uppercase tracking-widest text-slate-500"
      >
        {{ t("ws.sectionPlatform") }}
      </p>

      <router-link
        to="/system/doctor"
        data-testid="nav-system-doctor"
        :title="collapsed ? t('ws.nav.doctor') : undefined"
        :aria-label="t('ws.nav.doctor')"
        class="group flex items-center rounded-lg transition"
        :class="[
          collapsed ? 'h-9 w-9 justify-center' : 'w-full gap-2.5 px-2.5 py-2',
          activeSection === 'doctor'
            ? 'bg-indigo-600/90 text-white shadow-sm'
            : 'text-slate-300 hover:bg-slate-800 hover:text-white',
        ]"
      >
        <Wrench
          class="h-4 w-4 shrink-0"
          :class="activeSection === 'doctor' ? 'text-white' : 'text-slate-400 group-hover:text-slate-200'"
        />
        <div v-if="!collapsed" class="min-w-0 flex-1 overflow-hidden whitespace-nowrap">
          <div class="truncate text-xs font-medium leading-tight">{{ t("ws.nav.doctor") }}</div>
          <div
            class="mt-0.5 truncate text-[10px] leading-tight"
            :class="activeSection === 'doctor' ? 'text-indigo-100/80' : 'text-slate-500'"
          >
            {{ t("ws.nav.doctorHint") }}
          </div>
        </div>
      </router-link>

      <!-- Backend Status -->
      <div
        v-if="!collapsed"
        class="flex items-center justify-between overflow-hidden whitespace-nowrap rounded-lg bg-slate-800/70 px-3 py-1.5"
      >
        <div class="flex items-center gap-2">
          <Activity class="h-3.5 w-3.5 shrink-0 text-slate-400" />
          <span class="text-[11px] text-slate-300">{{ t("nav.backendCore") }}</span>
        </div>
        <span class="inline-flex items-center gap-1.5 text-[11px] font-medium">
          <span
            class="h-2 w-2 rounded-full"
            :class="
              apiOnline === true
                ? 'bg-emerald-400'
                : apiOnline === false
                  ? 'bg-rose-400'
                  : 'bg-slate-500 animate-pulse'
            "
          />
          <span
            :class="
              apiOnline === true
                ? 'text-emerald-300'
                : apiOnline === false
                  ? 'text-rose-300'
                  : 'text-slate-400'
            "
          >
            {{ apiStatusLabel }}
          </span>
        </span>
      </div>

      <div
        v-else
        class="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-800/70"
        :title="`${t('nav.backendCore')}: ${apiStatusLabel}`"
      >
        <span
          class="h-2.5 w-2.5 rounded-full"
          :class="
            apiOnline === true
              ? 'bg-emerald-400'
              : apiOnline === false
                ? 'bg-rose-400'
                : 'bg-slate-500 animate-pulse'
          "
        />
      </div>

      <button
        type="button"
        class="flex items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-800 hover:text-slate-100"
        :class="collapsed ? 'h-8 w-8' : 'w-full gap-2 px-3 py-1.5 text-[11px]'"
        :title="toggleLabel"
        :aria-label="toggleLabel"
        data-testid="sidebar-footer-toggle"
        @click="emit('toggle')"
      >
        <PanelLeftOpen v-if="collapsed" class="h-4 w-4 shrink-0" />
        <PanelLeftClose v-else class="h-4 w-4 shrink-0" />
        <span v-if="!collapsed" class="truncate whitespace-nowrap">{{ toggleLabel }}</span>
      </button>
    </div>

    <!-- Quick Create Project Modal -->
    <Teleport to="body">
      <div
        v-if="createModalOpen"
        class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
        data-testid="sidebar-create-project-modal"
        @click.self="createModalOpen = false"
      >
        <div class="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 text-slate-900 shadow-xl">
          <div class="flex items-center justify-between">
            <h3 class="text-base font-semibold text-slate-900">{{ t("projects.modal.title") }}</h3>
            <button
              type="button"
              class="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              @click="createModalOpen = false"
            >
              <X class="h-4 w-4" />
            </button>
          </div>
          <p class="mt-1 text-xs text-slate-500">{{ t("projects.modal.desc") }}</p>

          <div
            v-if="createError"
            class="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700"
          >
            {{ createError }}
          </div>

          <form class="mt-4 space-y-4" @submit.prevent="handleCreateProject">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.modal.nameLabel") }}
              </label>
              <input
                v-model="newName"
                type="text"
                required
                :placeholder="t('projects.modal.namePlaceholder')"
                class="input-field"
              />
            </div>

            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.modal.slugLabel") }}
                <span class="font-normal text-slate-400">{{ t("projects.modal.slugHint") }}</span>
              </label>
              <input
                v-model="newSlug"
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
                v-model="newDesc"
                rows="3"
                :placeholder="t('projects.modal.descPlaceholder')"
                class="input-field"
              />
            </div>

            <div class="flex items-center justify-end gap-2 pt-2">
              <button type="button" class="btn-secondary" @click="createModalOpen = false">
                {{ t("common.cancel") }}
              </button>
              <button type="submit" class="btn-primary" :disabled="creating">
                {{ creating ? t("projects.modal.creating") : t("projects.modal.submit") }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>
  </aside>
</template>
