import { createRouter, createWebHistory } from "vue-router";
import OverviewView from "@/views/OverviewView.vue";
import ProjectsView from "@/views/ProjectsView.vue";
import { useWorkspace, type WorkspaceSection } from "@/stores/workspace";

const LEGACY_REDIRECT_MAP: Record<string, WorkspaceSection> = {
  "legacy-overview": "overview",
  "legacy-explore": "explore",
  "legacy-scenarios": "scenarios",
  "legacy-runs": "runs",
  "legacy-trends": "trends",
};

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // Legacy root entrypoints — gracefully redirected to active project workspace /p/:projectId/...
    {
      path: "/",
      name: "legacy-overview",
      component: OverviewView,
      meta: { titleKey: "route.projectOverview.title", subtitleKey: "route.projectOverview.subtitle" },
    },
    {
      path: "/explore",
      name: "legacy-explore",
      component: () => import("@/views/ExploreView.vue"),
      meta: { titleKey: "route.projectExplore.title", subtitleKey: "route.projectExplore.subtitle" },
    },
    {
      path: "/scenarios",
      name: "legacy-scenarios",
      component: () => import("@/views/ScenariosView.vue"),
      meta: {
        titleKey: "route.projectScenarios.title",
        subtitleKey: "route.projectScenarios.subtitle",
      },
    },
    {
      path: "/runs",
      name: "legacy-runs",
      component: () => import("@/views/RunsView.vue"),
      meta: { titleKey: "route.projectRuns.title", subtitleKey: "route.projectRuns.subtitle" },
    },
    {
      path: "/trends",
      name: "legacy-trends",
      component: () => import("@/views/TrendsView.vue"),
      meta: { titleKey: "route.projectTrends.title", subtitleKey: "route.projectTrends.subtitle" },
    },

    // Backward-compatible direct run link
    {
      path: "/runs/:runId",
      name: "run-detail",
      component: () => import("@/views/RunDetailView.vue"),
      meta: { titleKey: "route.runDetail.title", subtitleKey: "route.runDetail.subtitle" },
    },

    // Platform-level routes
    {
      path: "/projects",
      name: "projects",
      component: ProjectsView,
      meta: {
        titleKey: "route.projectsDirectory.title",
        subtitleKey: "route.projectsDirectory.subtitle",
        platformScope: true,
      },
    },
    {
      path: "/system/doctor",
      name: "system-doctor",
      component: () => import("@/views/DoctorView.vue"),
      meta: {
        titleKey: "route.systemDoctor.title",
        subtitleKey: "route.systemDoctor.subtitle",
        platformScope: true,
      },
    },

    // Project-scoped Jira Workspace routes (/p/:projectId/...)
    {
      path: "/p/:projectId",
      redirect: (to) => `/p/${encodeURIComponent(String(to.params.projectId))}/overview`,
    },
    {
      path: "/p/:projectId/overview",
      name: "project-overview",
      component: OverviewView,
      meta: {
        titleKey: "route.projectOverview.title",
        subtitleKey: "route.projectOverview.subtitle",
        workspaceSection: "overview",
      },
    },
    {
      path: "/p/:projectId/explore",
      name: "project-explore",
      component: () => import("@/views/ExploreView.vue"),
      meta: {
        titleKey: "route.projectExplore.title",
        subtitleKey: "route.projectExplore.subtitle",
        workspaceSection: "explore",
      },
    },
    {
      path: "/p/:projectId/inventory",
      name: "project-inventory",
      component: () => import("@/views/InventoryView.vue"),
      meta: {
        titleKey: "route.projectInventory.title",
        subtitleKey: "route.projectInventory.subtitle",
        workspaceSection: "inventory",
      },
    },
    {
      path: "/p/:projectId/scenarios",
      name: "workspace-scenarios",
      component: () => import("@/views/ScenariosView.vue"),
      meta: {
        titleKey: "route.projectScenarios.title",
        subtitleKey: "route.projectScenarios.subtitle",
        workspaceSection: "scenarios",
      },
    },
    {
      path: "/p/:projectId/runs",
      name: "project-runs",
      component: () => import("@/views/RunsView.vue"),
      meta: {
        titleKey: "route.projectRuns.title",
        subtitleKey: "route.projectRuns.subtitle",
        workspaceSection: "runs",
      },
    },
    {
      path: "/p/:projectId/runs/:runId",
      name: "project-run-detail",
      component: () => import("@/views/RunDetailView.vue"),
      meta: {
        titleKey: "route.runDetail.title",
        subtitleKey: "route.runDetail.subtitle",
        workspaceSection: "runs",
      },
    },
    {
      path: "/p/:projectId/kb",
      name: "project-kb",
      component: () => import("@/views/KnowledgeBaseView.vue"),
      meta: {
        titleKey: "route.projectKb.title",
        subtitleKey: "route.projectKb.subtitle",
        workspaceSection: "kb",
      },
    },
    {
      path: "/p/:projectId/trends",
      name: "project-trends",
      component: () => import("@/views/TrendsView.vue"),
      meta: {
        titleKey: "route.projectTrends.title",
        subtitleKey: "route.projectTrends.subtitle",
        workspaceSection: "trends",
      },
    },
    {
      path: "/p/:projectId/settings",
      name: "project-settings",
      component: () => import("@/views/ProjectSettingsView.vue"),
      meta: {
        titleKey: "route.projectSettings.title",
        subtitleKey: "route.projectSettings.subtitle",
        workspaceSection: "settings",
      },
    },
  ],
});

router.beforeEach(async (to) => {
  const ws = useWorkspace();
  const routeName = String(to.name || "");

  // Handle legacy root redirects -> /p/:activeProjectId/<section>
  const legacySection = LEGACY_REDIRECT_MAP[routeName];
  if (legacySection) {
    await ws.ensureLoaded();
    const queryProj = typeof to.query.projectId === "string" ? to.query.projectId : "";
    const targetPid =
      (queryProj && ws.projects.value.some((p) => p.id === queryProj) ? queryProj : "") ||
      ws.activeProjectId.value ||
      ws.projects.value[0]?.id ||
      "";
    if (targetPid) {
      ws.setActiveProject(targetPid);
      const nextQuery = { ...to.query };
      delete nextQuery.projectId;
      return {
        path: `/p/${encodeURIComponent(targetPid)}/${legacySection}`,
        query: nextQuery,
        replace: true,
      };
    }
    // Zero projects exist: allow /runs?new=1 to still work (zero-friction), else fall back to /projects
    if (legacySection === "runs" && to.query.new === "1") {
      return true;
    }
    return { path: "/projects", replace: true };
  }

  // Sync activeProjectId whenever entering /p/:projectId/...
  const paramPid = typeof to.params.projectId === "string" ? to.params.projectId : "";
  if (paramPid) {
    if (ws.activeProjectId.value !== paramPid) {
      ws.setActiveProject(paramPid);
    }
    if (!ws.initialized.value) {
      void ws.ensureLoaded();
    }
  } else if (!ws.initialized.value) {
    void ws.ensureLoaded();
  }

  return true;
});

export default router;
