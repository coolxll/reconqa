import { computed, ref } from "vue";
import type { RouteLocationNormalizedLoaded, Router } from "vue-router";
import {
  createProject as apiCreateProject,
  deleteProject as apiDeleteProject,
  listProjects,
  updateProject as apiUpdateProject,
} from "@/api/projects";
import type { Project, ProjectCreate, ProjectUpdate } from "@/api/types";

export type WorkspaceSection =
  | "overview"
  | "explore"
  | "inventory"
  | "scenarios"
  | "runs"
  | "kb"
  | "trends"
  | "settings";

const STORAGE_KEY = "reconqa_active_project_id";

function readStoredProjectId(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function writeStoredProjectId(id: string): void {
  if (typeof window === "undefined") return;
  try {
    if (id) {
      window.localStorage.setItem(STORAGE_KEY, id);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // Ignore storage access errors
  }
}

const projects = ref<Project[]>([]);
const activeProjectId = ref<string>(readStoredProjectId());
const loadingProjects = ref(false);
const initialized = ref(false);
const workspaceError = ref<string | null>(null);

let inflightLoad: Promise<Project[]> | null = null;

const activeProject = computed<Project | null>(
  () => projects.value.find((p) => p.id === activeProjectId.value) ?? null
);

function setActiveProject(id: string): void {
  activeProjectId.value = id;
  writeStoredProjectId(id);
}

async function loadProjects(force = false): Promise<Project[]> {
  if (inflightLoad && !force) {
    return inflightLoad;
  }
  loadingProjects.value = true;
  workspaceError.value = null;

  const task = listProjects()
    .then((list) => {
      projects.value = list;
      initialized.value = true;
      if (list.length === 0) {
        setActiveProject("");
      } else if (!activeProjectId.value || !list.some((p) => p.id === activeProjectId.value)) {
        const stored = readStoredProjectId();
        if (stored && list.some((p) => p.id === stored)) {
          setActiveProject(stored);
        } else {
          setActiveProject(list[0].id);
        }
      }
      return list;
    })
    .catch((err) => {
      workspaceError.value = err instanceof Error ? err.message : "Failed to load projects";
      initialized.value = true;
      return projects.value;
    })
    .finally(() => {
      loadingProjects.value = false;
      inflightLoad = null;
    });

  inflightLoad = task;
  return task;
}

async function ensureLoaded(): Promise<Project[]> {
  if (initialized.value && projects.value.length > 0) {
    return projects.value;
  }
  return loadProjects(false);
}

function resolveSectionFromRoute(route?: RouteLocationNormalizedLoaded): WorkspaceSection {
  const name = String(route?.name || "");
  if (name === "project-explore") return "explore";
  if (name === "project-inventory") return "inventory";
  if (name === "workspace-scenarios") return "scenarios";
  if (name === "project-runs" || name === "project-run-detail") return "runs";
  if (name === "project-kb") return "kb";
  if (name === "project-trends") return "trends";
  if (name === "project-settings") return "settings";
  return "overview";
}

function workspacePath(section: WorkspaceSection, projectId?: string | null): string {
  const pid = projectId || activeProjectId.value;
  if (!pid) {
    return "/projects";
  }
  return `/p/${encodeURIComponent(pid)}/${section}`;
}

async function selectProject(
  id: string,
  router?: Router,
  currentRoute?: RouteLocationNormalizedLoaded,
  targetSection?: WorkspaceSection
): Promise<void> {
  if (!id) return;
  setActiveProject(id);
  if (router) {
    const section = targetSection ?? resolveSectionFromRoute(currentRoute);
    await router.push(workspacePath(section, id));
  }
}

async function createProject(
  payload: ProjectCreate,
  router?: Router,
  navigateToOverview = true
): Promise<Project> {
  const created = await apiCreateProject(payload);
  projects.value = [created, ...projects.value.filter((p) => p.id !== created.id)];
  initialized.value = true;
  setActiveProject(created.id);
  if (router && navigateToOverview) {
    await router.push(workspacePath("overview", created.id));
  }
  return created;
}

async function updateProject(id: string, payload: ProjectUpdate): Promise<Project> {
  const updated = await apiUpdateProject(id, payload);
  upsertProject(updated);
  return updated;
}

async function deleteProject(id: string, router?: Router): Promise<void> {
  await apiDeleteProject(id);
  projects.value = projects.value.filter((p) => p.id !== id);
  if (activeProjectId.value === id) {
    const next = projects.value[0]?.id ?? "";
    setActiveProject(next);
    if (router) {
      await router.push(next ? workspacePath("overview", next) : "/projects");
    }
  }
}

function upsertProject(project: Project): void {
  const exists = projects.value.some((p) => p.id === project.id);
  if (exists) {
    projects.value = projects.value.map((p) => (p.id === project.id ? project : p));
  } else {
    projects.value = [project, ...projects.value];
  }
}

export function useWorkspace() {
  return {
    projects,
    activeProjectId,
    activeProject,
    loadingProjects,
    initialized,
    workspaceError,
    loadProjects,
    ensureLoaded,
    setActiveProject,
    selectProject,
    createProject,
    updateProject,
    deleteProject,
    upsertProject,
    workspacePath,
  };
}
