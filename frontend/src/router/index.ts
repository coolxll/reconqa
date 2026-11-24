import { createRouter, createWebHistory } from "vue-router";
import OverviewView from "@/views/OverviewView.vue";
import ProjectsView from "@/views/ProjectsView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      name: "overview",
      component: OverviewView,
      meta: {
        title: "Overview",
        subtitle: "One-Recording, Multi-Use Web/API Quality & Security Console",
      },
    },
    {
      path: "/projects",
      name: "projects",
      component: ProjectsView,
      meta: {
        title: "Projects & Knowledge Base",
        subtitle: "Organize project knowledge docs, target scopes, and reusable credentials",
      },
    },
    {
      path: "/explore",
      name: "explore",
      component: () => import("@/views/ExploreView.vue"),
      meta: {
        title: "Explore & Discovery",
        subtitle: "Discover hidden endpoints from a seed cURL, OpenAPI/Swagger probes, and JS bundles",
      },
    },
    {
      path: "/runs",
      name: "runs",
      component: () => import("@/views/RunsView.vue"),
      meta: {
        title: "Pipeline Runs",
        subtitle: "Orchestrate HAR ingestion, OpenAPI synthesis, Java codegen, security & k6",
      },
    },
    {
      path: "/runs/:runId",
      name: "run-detail",
      component: () => import("@/views/RunDetailView.vue"),
      meta: {
        title: "Run Inspector",
        subtitle: "Live SSE stream, OpenAPI explorer, correlation graph, security & performance reports",
      },
    },
    {
      path: "/trends",
      name: "trends",
      component: () => import("@/views/TrendsView.vue"),
      meta: {
        title: "Historical Trends",
        subtitle: "Track API coverage growth, finding deltas, and performance regressions across runs",
      },
    },
  ],
});

export default router;
