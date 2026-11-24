<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  ChevronRight,
  Compass,
  FileCode2,
  Globe,
  History,
  KeyRound,
  Layers,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Terminal,
} from "lucide-vue-next";
import {
  getExplore,
  listExplores,
  promoteExploreToRun,
  runExplore,
  type DiscoveredEndpoint,
  type ExploreListItem,
  type ExploreMode,
  type ExploreResult,
} from "@/api/explore";
import { listProjects } from "@/api/projects";
import type { Project } from "@/api/types";
import EmptyState from "@/components/layout/EmptyState.vue";
import MetricCard from "@/components/layout/MetricCard.vue";
import { useI18n } from "@/i18n";
import "@/i18n/locales/explore";

const SAMPLE_CURL = `curl 'https://api.example.com/api/v1/orders/1001' \\
  -H 'Accept: application/json' \\
  -H 'Authorization: Bearer demo-session-token' \\
  -H 'X-Client-Platform: web'`;

const AUTH_HEADER_PATTERN = /^(authorization|cookie|x-auth-token|x-api-key|x-token|token)$/i;

const router = useRouter();
const { t } = useI18n();

// Seed input state
const inputMode = ref<"curl" | "url">("curl");
const seedCurl = ref("");
const targetUrl = ref("");
const discoveryMode = ref<ExploreMode>("passive");
const probeOpenApiDocs = ref(true);
const crawlJs = ref(true);
const depth = ref(2);

// Execution & history state
const running = ref(false);
const loadingHistory = ref(false);
const promoting = ref(false);
const error = ref<string | null>(null);
const historyItems = ref<ExploreListItem[]>([]);
const selectedHistoryId = ref("");
const result = ref<ExploreResult | null>(null);

// Result explorer state
const activeResultTab = ref<"endpoints" | "spa" | "assets" | "warnings">("endpoints");
const sourceFilter = ref<string>("all");
const endpointSearch = ref("");
const selectedEndpointIds = ref<Set<string>>(new Set());
const expandedEndpointIds = ref<Set<string>>(new Set());

// Promote to Test Run state
const projects = ref<Project[]>([]);
const promoteProjectSelection = ref<string>("__auto__");
const customProjectName = ref("");
const promoteJava = ref(true);
const promotePerf = ref(true);
const promoteSecurity = ref(true);

const parsedSeedPreview = computed(() => {
  const raw = seedCurl.value.trim();
  if (!raw) {
    return { baseUrl: "", rootDomain: "", authHeaders: [] as string[] };
  }

  let baseUrl = "";
  let rootDomain = "";
  const urlMatch = raw.match(/https?:\/\/[^\s'"\\]+/i);
  if (urlMatch) {
    try {
      const parsed = new URL(urlMatch[0]);
      baseUrl = parsed.origin;
      const parts = parsed.hostname.split(".").filter(Boolean);
      rootDomain =
        parts.length >= 2 && !/^\d+\.\d+\.\d+\.\d+$/.test(parsed.hostname)
          ? parts.slice(-2).join(".")
          : parsed.hostname;
    } catch {
      // Ignore malformed partial URL while typing
    }
  }

  const authHeaders = new Set<string>();
  const headerRegex = /(?:-H|--header)\s+(?:\$?'([^']+)'|"([^"]+)")/g;
  let hm: RegExpExecArray | null = headerRegex.exec(raw);
  while (hm) {
    const hdr = (hm[1] ?? hm[2] ?? "").trim();
    const colon = hdr.indexOf(":");
    if (colon > 0) {
      const name = hdr.slice(0, colon).trim();
      if (AUTH_HEADER_PATTERN.test(name)) {
        authHeaders.add(name);
      }
    }
    hm = headerRegex.exec(raw);
  }
  if (/(?:^|\s)(?:-b|--cookie)\s+/.test(raw)) {
    authHeaders.add("Cookie");
  }

  return {
    baseUrl,
    rootDomain,
    authHeaders: Array.from(authHeaders),
  };
});

const endpoints = computed<DiscoveredEndpoint[]>(() => result.value?.discovered_endpoints ?? []);

const sourceCounts = computed(() => {
  const map = new Map<string, number>();
  for (const ep of endpoints.value) {
    const src = ep.source || "unknown";
    map.set(src, (map.get(src) ?? 0) + 1);
  }
  return Array.from(map.entries()).map(([source, count]) => ({ source, count }));
});

const filteredEndpoints = computed(() => {
  const q = endpointSearch.value.trim().toLowerCase();
  return endpoints.value.filter((ep) => {
    if (sourceFilter.value !== "all" && ep.source !== sourceFilter.value) {
      return false;
    }
    if (!q) return true;
    return (
      ep.method.toLowerCase().includes(q) ||
      ep.path.toLowerCase().includes(q) ||
      ep.url.toLowerCase().includes(q) ||
      ep.source.toLowerCase().includes(q) ||
      (ep.inferred_params ?? []).some((p) => p.toLowerCase().includes(q))
    );
  });
});

const allFilteredSelected = computed(() => {
  if (filteredEndpoints.value.length === 0) return false;
  return filteredEndpoints.value.every((ep) => selectedEndpointIds.value.has(ep.id));
});

function sourceBadgeClass(source: string): string {
  switch (source) {
    case "seed_curl":
      return "bg-indigo-50 text-indigo-700 ring-indigo-600/20";
    case "openapi_probe":
      return "bg-emerald-50 text-emerald-700 ring-emerald-600/20";
    case "js_static_inferred":
      return "bg-violet-50 text-violet-700 ring-violet-600/20";
    case "js_static":
      return "bg-amber-50 text-amber-800 ring-amber-600/20";
    case "katana":
    case "urlfinder":
      return "bg-sky-50 text-sky-700 ring-sky-600/20";
    default:
      return "bg-slate-100 text-slate-700 ring-slate-500/20";
  }
}

function methodBadgeClass(method: string): string {
  switch (method.toUpperCase()) {
    case "GET":
      return "bg-sky-50 text-sky-700 ring-sky-200";
    case "POST":
      return "bg-emerald-50 text-emerald-700 ring-emerald-200";
    case "PUT":
    case "PATCH":
      return "bg-amber-50 text-amber-700 ring-amber-200";
    case "DELETE":
      return "bg-rose-50 text-rose-700 ring-rose-200";
    default:
      return "bg-slate-100 text-slate-700 ring-slate-200";
  }
}

function formatShortTime(iso: string | undefined): string {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  const now = new Date();
  const sameDay =
    dt.getFullYear() === now.getFullYear() &&
    dt.getMonth() === now.getMonth() &&
    dt.getDate() === now.getDate();
  const hh = String(dt.getHours()).padStart(2, "0");
  const mi = String(dt.getMinutes()).padStart(2, "0");
  if (sameDay) {
    return `${hh}:${mi}`;
  }
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  const dd = String(dt.getDate()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

function historyOptionLabel(item: ExploreListItem): string {
  const domain =
    item.seed?.root_domain ||
    item.seed?.base_url ||
    item.root_domain ||
    item.base_url ||
    "target";
  const count = item.summary?.endpoints_count ?? item.endpoints_count ?? 0;
  const timeStr = formatShortTime(item.created_at);
  if (timeStr) {
    return t("explore.historyOptionFmt", { domain, count, time: timeStr });
  }
  return t("explore.historyOptionNoTime", { domain, count });
}

function startNewExplore(): void {
  selectedHistoryId.value = "";
  result.value = null;
  error.value = null;
  selectedEndpointIds.value = new Set();
  expandedEndpointIds.value = new Set();
}

function formatAssetEntry(entry: Record<string, unknown> | string): string {
  if (typeof entry === "string") return entry;
  const url = entry.url ?? entry.host ?? entry.name ?? entry.entry;
  const status = entry.status_code ?? entry.status;
  const title = entry.title;
  if (url) {
    const parts = [String(url)];
    if (status) parts.push(`[${String(status)}]`);
    if (title) parts.push(String(title));
    return parts.join(" ");
  }
  return JSON.stringify(entry);
}

function applyResult(res: ExploreResult): void {
  result.value = res;
  selectedHistoryId.value = res.explore_id;
  sourceFilter.value = "all";
  endpointSearch.value = "";
  activeResultTab.value = "endpoints";
  expandedEndpointIds.value = new Set();
  selectedEndpointIds.value = new Set((res.discovered_endpoints ?? []).map((ep) => ep.id));
}

function loadSampleCurl(): void {
  inputMode.value = "curl";
  seedCurl.value = SAMPLE_CURL;
  error.value = null;
}

function toggleEndpointSelection(id: string): void {
  const next = new Set(selectedEndpointIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selectedEndpointIds.value = next;
}

function toggleAllFiltered(): void {
  const next = new Set(selectedEndpointIds.value);
  if (allFilteredSelected.value) {
    for (const ep of filteredEndpoints.value) next.delete(ep.id);
  } else {
    for (const ep of filteredEndpoints.value) next.add(ep.id);
  }
  selectedEndpointIds.value = next;
}

function clearSelection(): void {
  selectedEndpointIds.value = new Set();
}

function toggleExpandEndpoint(id: string): void {
  const next = new Set(expandedEndpointIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  expandedEndpointIds.value = next;
}

async function fetchHistoryAndProjects(): Promise<void> {
  loadingHistory.value = true;
  const [histRes, projRes] = await Promise.allSettled([listExplores(), listProjects()]);
  if (histRes.status === "fulfilled") {
    historyItems.value = histRes.value;
    if (!result.value && histRes.value.length > 0) {
      const firstId = histRes.value[0].explore_id;
      try {
        const full = await getExplore(firstId);
        applyResult(full);
      } catch {
        // Ignore initial history auto-load failure
      }
    }
  }
  if (projRes.status === "fulfilled") {
    projects.value = projRes.value;
  }
  loadingHistory.value = false;
}

async function onSelectHistory(exploreId: string): Promise<void> {
  if (!exploreId) {
    startNewExplore();
    return;
  }
  error.value = null;
  loadingHistory.value = true;
  try {
    const full = await getExplore(exploreId);
    applyResult(full);
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("explore.errorLoad");
  } finally {
    loadingHistory.value = false;
  }
}

async function handleRunExplore(): Promise<void> {
  error.value = null;
  const curlText = inputMode.value === "curl" ? seedCurl.value.trim() : "";
  const urlText = inputMode.value === "url" ? targetUrl.value.trim() : "";

  if (!curlText && !urlText) {
    error.value = t("explore.validationMissingInput");
    return;
  }

  running.value = true;
  try {
    const res = await runExplore({
      seed_curl: curlText || null,
      target_url: urlText || null,
      mode: discoveryMode.value,
      probe_openapi_docs: probeOpenApiDocs.value,
      crawl_js: crawlJs.value,
      depth: depth.value,
    });
    applyResult(res);
    const updatedList = await listExplores().catch(() => historyItems.value);
    historyItems.value = updatedList;
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("explore.errorRun");
  } finally {
    running.value = false;
  }
}

async function handlePromoteToRun(): Promise<void> {
  if (!result.value) return;
  const modules = ["merge"];
  if (promoteJava.value) modules.push("java_gen");
  if (promotePerf.value) modules.push("perf");
  if (promoteSecurity.value) modules.push("security");

  if (modules.length === 1) {
    error.value = t("explore.promoteNeedOneCap");
    return;
  }

  promoting.value = true;
  error.value = null;
  try {
    const selected = Array.from(selectedEndpointIds.value);
    const payload = {
      project_id:
        promoteProjectSelection.value !== "__auto__" &&
        promoteProjectSelection.value !== "__new__"
          ? promoteProjectSelection.value
          : null,
      project_name:
        promoteProjectSelection.value === "__new__" && customProjectName.value.trim()
          ? customProjectName.value.trim()
          : null,
      modules,
      selected_endpoints: selected.length > 0 ? selected : null,
    };
    const createdRun = await promoteExploreToRun(result.value.explore_id, payload);
    await router.push(`/runs/${createdRun.id}`);
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("explore.errorPromote");
  } finally {
    promoting.value = false;
  }
}

onMounted(() => {
  void fetchHistoryAndProjects();
});
</script>

<template>
  <div class="space-y-6 pb-8">
    <!-- Top Header & History Picker -->
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <Compass class="h-5 w-5 text-indigo-600" />
          <h1 class="text-xl font-semibold tracking-tight text-slate-900">
            {{ t("explore.title") }}
          </h1>
        </div>
        <p class="mt-1 max-w-3xl text-xs leading-relaxed text-slate-500">
          {{ t("explore.subtitle") }}
        </p>
      </div>

      <div
        v-if="historyItems.length > 0"
        data-testid="explore-history-bar"
        class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 shadow-sm"
      >
        <div class="flex items-center gap-1.5 text-xs font-medium text-slate-600">
          <History class="h-3.5 w-3.5 text-slate-400" />
          <span>{{ t("explore.historyLabel") }}:</span>
        </div>
        <select
          v-model="selectedHistoryId"
          data-testid="explore-history-select"
          class="input-field w-72 py-1 text-xs"
          :aria-label="t('explore.historyLabel')"
          :disabled="loadingHistory"
          @change="onSelectHistory(selectedHistoryId)"
        >
          <option value="">{{ t("explore.newExploreOpt") }}</option>
          <option v-for="item in historyItems" :key="item.explore_id" :value="item.explore_id">
            {{ historyOptionLabel(item) }}
          </option>
        </select>
        <button
          v-if="result"
          type="button"
          data-testid="explore-new-btn"
          class="btn-secondary py-1 text-xs"
          @click="startNewExplore"
        >
          <Plus class="h-3.5 w-3.5" />
          <span>{{ t("explore.newExploreBtn") }}</span>
        </button>
        <button
          type="button"
          class="btn-secondary py-1 text-xs"
          :disabled="loadingHistory"
          @click="fetchHistoryAndProjects"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="loadingHistory ? 'animate-spin' : ''" />
          <span>{{ t("explore.refreshHistory") }}</span>
        </button>
      </div>
    </div>

    <!-- Seed Input Card -->
    <div class="card overflow-hidden">
      <!-- Input Mode Tabs -->
      <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 bg-slate-50/70 px-5 py-3">
        <div class="flex items-center gap-1.5">
          <button
            type="button"
            data-testid="explore-tab-curl"
            class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition"
            :class="
              inputMode === 'curl'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:text-slate-900'
            "
            @click="inputMode = 'curl'"
          >
            <Terminal class="h-3.5 w-3.5" />
            <span>{{ t("explore.tabSeedCurl") }}</span>
          </button>
          <button
            type="button"
            data-testid="explore-tab-url"
            class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition"
            :class="
              inputMode === 'url'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:text-slate-900'
            "
            @click="inputMode = 'url'"
          >
            <Globe class="h-3.5 w-3.5" />
            <span>{{ t("explore.tabDomainUrl") }}</span>
          </button>
        </div>

        <div v-if="inputMode === 'curl'" class="flex items-center gap-2">
          <button
            type="button"
            data-testid="explore-load-sample"
            class="btn-secondary px-2.5 py-1 text-xs"
            @click="loadSampleCurl"
          >
            <Sparkles class="h-3.5 w-3.5 text-indigo-600" />
            <span>{{ t("explore.loadSample") }}</span>
          </button>
          <button
            v-if="seedCurl"
            type="button"
            class="btn-secondary px-2.5 py-1 text-xs"
            @click="seedCurl = ''"
          >
            {{ t("explore.clearBtn") }}
          </button>
        </div>
      </div>

      <div class="space-y-4 p-5">
        <!-- Tab 1: Seed cURL -->
        <div v-if="inputMode === 'curl'" class="space-y-3">
          <p class="text-xs text-slate-500">{{ t("explore.curlHint") }}</p>
          <textarea
            v-model="seedCurl"
            data-testid="explore-curl-input"
            rows="4"
            class="input-field font-mono text-xs leading-relaxed"
            :placeholder="t('explore.curlPlaceholder')"
          />

          <!-- Live Detected Preview Pills -->
          <div
            v-if="parsedSeedPreview.baseUrl || parsedSeedPreview.authHeaders.length > 0"
            class="flex flex-wrap items-center gap-2 rounded-lg border border-indigo-100 bg-indigo-50/50 px-3.5 py-2 text-xs"
          >
            <span v-if="parsedSeedPreview.baseUrl" class="inline-flex items-center gap-1 text-slate-700">
              <Globe class="h-3.5 w-3.5 text-indigo-600" />
              <span class="text-slate-500">{{ t("explore.detectedBaseUrl") }}:</span>
              <code class="rounded bg-white px-1.5 py-0.5 font-mono font-medium text-indigo-700 ring-1 ring-indigo-200">
                {{ parsedSeedPreview.baseUrl }}
              </code>
            </span>
            <span v-if="parsedSeedPreview.rootDomain" class="inline-flex items-center gap-1 text-slate-700">
              <span class="text-slate-500">{{ t("explore.detectedDomain") }}:</span>
              <code class="rounded bg-white px-1.5 py-0.5 font-mono text-slate-800 ring-1 ring-slate-200">
                {{ parsedSeedPreview.rootDomain }}
              </code>
            </span>
            <span class="inline-flex flex-wrap items-center gap-1 text-slate-700">
              <KeyRound class="h-3.5 w-3.5 text-emerald-600" />
              <template v-if="parsedSeedPreview.authHeaders.length > 0">
                <span class="text-slate-500">{{ t("explore.detectedAuth") }}:</span>
                <span
                  v-for="hdr in parsedSeedPreview.authHeaders"
                  :key="hdr"
                  class="rounded-full bg-emerald-50 px-2 py-0.5 font-mono text-[11px] font-medium text-emerald-700 ring-1 ring-emerald-600/20"
                >
                  {{ hdr }}
                </span>
              </template>
              <span v-else class="text-slate-500">{{ t("explore.noAuthDetected") }}</span>
            </span>
          </div>
        </div>

        <!-- Tab 2: Domain / URL -->
        <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div class="sm:col-span-2">
            <label class="block text-xs font-medium text-slate-700">
              {{ t("explore.targetUrlLabel") }}
            </label>
            <input
              v-model="targetUrl"
              data-testid="explore-url-input"
              type="text"
              class="input-field mt-1 font-mono text-xs"
              :placeholder="t('explore.targetUrlPlaceholder')"
              @keyup.enter="handleRunExplore"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-slate-700">
              {{ t("explore.modeLabel") }}
            </label>
            <div class="mt-1 flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-xs">
              <button
                type="button"
                class="flex-1 rounded-md px-2 py-1.5 font-medium transition"
                :class="
                  discoveryMode === 'passive'
                    ? 'bg-white text-indigo-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900'
                "
                @click="discoveryMode = 'passive'"
              >
                Passive
              </button>
              <button
                type="button"
                class="flex-1 rounded-md px-2 py-1.5 font-medium transition"
                :class="
                  discoveryMode === 'active'
                    ? 'bg-white text-indigo-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900'
                "
                @click="discoveryMode = 'active'"
              >
                Active
              </button>
            </div>
          </div>
        </div>

        <!-- Strategy Checkboxes + Launch Button -->
        <div class="flex flex-wrap items-center justify-between gap-4 border-t border-slate-100 pt-4">
          <div class="flex flex-wrap items-center gap-4 text-xs">
            <label class="flex cursor-pointer items-start gap-2">
              <input
                v-model="probeOpenApiDocs"
                type="checkbox"
                class="mt-0.5 h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
              />
              <span>
                <span class="block font-medium text-slate-800">{{ t("explore.optOpenApi") }}</span>
                <span class="text-[11px] text-slate-500">{{ t("explore.optOpenApiDesc") }}</span>
              </span>
            </label>

            <label class="flex cursor-pointer items-start gap-2">
              <input
                v-model="crawlJs"
                type="checkbox"
                class="mt-0.5 h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
              />
              <span>
                <span class="block font-medium text-slate-800">{{ t("explore.optCrawlJs") }}</span>
                <span class="text-[11px] text-slate-500">{{ t("explore.optCrawlJsDesc") }}</span>
              </span>
            </label>

            <div class="flex items-center gap-2">
              <span class="font-medium text-slate-700">{{ t("explore.optExternal") }}:</span>
              <select v-model="discoveryMode" class="input-field w-auto py-1 text-xs">
                <option value="passive">{{ t("explore.modePassive") }}</option>
                <option value="active">{{ t("explore.modeActive") }}</option>
              </select>
            </div>

            <div class="flex items-center gap-1.5">
              <span class="font-medium text-slate-700">{{ t("explore.depthLabel") }}:</span>
              <select v-model.number="depth" class="input-field w-16 py-1 text-xs">
                <option :value="1">1</option>
                <option :value="2">2</option>
                <option :value="3">3</option>
              </select>
            </div>
          </div>

          <button
            type="button"
            data-testid="explore-submit-btn"
            class="btn-primary px-5 py-2 text-sm"
            :disabled="running"
            @click="handleRunExplore"
          >
            <Loader2 v-if="running" class="h-4 w-4 animate-spin" />
            <Compass v-else class="h-4 w-4" />
            <span>{{ running ? t("explore.runningBtn") : t("explore.startBtn") }}</span>
          </button>
        </div>

        <!-- Error Banner -->
        <div
          v-if="error"
          class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-700"
        >
          {{ error }}
        </div>
      </div>
    </div>

    <!-- Empty State before any Explore -->
    <EmptyState
      v-if="!result && !running"
      :title="t('explore.emptyTitle')"
      :description="t('explore.emptyDesc')"
    >
      <template #icon>
        <Compass class="h-5 w-5 text-indigo-500" />
      </template>
      <template #action>
        <button type="button" class="btn-secondary text-xs" @click="loadSampleCurl">
          <Sparkles class="h-3.5 w-3.5 text-indigo-600" />
          <span>{{ t("explore.loadSample") }}</span>
        </button>
      </template>
    </EmptyState>

    <!-- Results Workbench -->
    <template v-else-if="result">
      <!-- Seed Summary Strip -->
      <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-5 py-3 text-xs shadow-sm">
        <div class="flex flex-wrap items-center gap-3">
          <span class="rounded-md bg-indigo-50 px-2 py-0.5 font-mono font-semibold text-indigo-700">
            {{ result.explore_id.slice(0, 12) }}
          </span>
          <span v-if="result.seed.base_url" class="font-mono text-slate-800">
            {{ result.seed.base_url }}
          </span>
          <span v-if="result.seed.api_prefix" class="text-slate-500">
            {{ t("explore.seedBannerPrefix") }}:
            <code class="ml-1 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-700">
              {{ result.seed.api_prefix }}
            </code>
          </span>
          <span
            v-for="hdr in result.seed.auth_headers_detected || []"
            :key="hdr"
            class="rounded-full bg-emerald-50 px-2 py-0.5 font-mono text-[11px] font-medium text-emerald-700 ring-1 ring-emerald-600/20"
          >
            {{ hdr }}
          </span>
        </div>

        <div v-if="(result.openapi_docs_found || []).length > 0" class="flex flex-wrap items-center gap-1.5">
          <span class="text-slate-500">{{ t("explore.seedBannerOpenApi") }}:</span>
          <span
            v-for="doc in result.openapi_docs_found"
            :key="doc.url"
            class="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2.5 py-0.5 font-mono text-[11px] text-emerald-800 ring-1 ring-emerald-200"
          >
            <FileCode2 class="h-3 w-3" />
            <span>{{ doc.title || doc.url }} ({{ doc.endpoints_count }})</span>
          </span>
        </div>
      </div>

      <!-- KPI Cards -->
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          :label="t('explore.kpiEndpoints')"
          :value="result.summary.endpoints_count ?? endpoints.length"
          :subtitle="t('explore.kpiEndpointsSub')"
          tone="indigo"
        />
        <MetricCard
          :label="t('explore.kpiOpenApi')"
          :value="result.summary.openapi_docs_count ?? result.openapi_docs_found.length"
          :subtitle="t('explore.kpiOpenApiSub')"
          tone="emerald"
        />
        <MetricCard
          :label="t('explore.kpiSpa')"
          :value="result.summary.spa_routes_count ?? result.spa_routes.length"
          :subtitle="t('explore.kpiSpaSub')"
          tone="neutral"
        />
        <MetricCard
          :label="t('explore.kpiAssets')"
          :value="`${result.summary.subdomains_count ?? result.subdomains.length} / ${result.summary.live_hosts_count ?? result.live_hosts.length}`"
          :subtitle="t('explore.kpiAssetsSub')"
          tone="amber"
        />
      </div>

      <!-- Main Results Card with 4 Tabs -->
      <div class="card overflow-hidden">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-slate-50/70 px-5 pt-2">
          <div class="flex flex-wrap items-center gap-1">
            <button
              type="button"
              class="border-b-2 px-3 py-2.5 text-xs font-medium transition"
              :class="
                activeResultTab === 'endpoints'
                  ? 'border-indigo-600 text-indigo-700'
                  : 'border-transparent text-slate-600 hover:text-slate-900'
              "
              @click="activeResultTab = 'endpoints'"
            >
              {{ t("explore.resTabEndpoints") }}
              <span class="ml-1 rounded-full bg-slate-200/80 px-1.5 py-0.5 text-[10px]">
                {{ endpoints.length }}
              </span>
            </button>
            <button
              type="button"
              class="border-b-2 px-3 py-2.5 text-xs font-medium transition"
              :class="
                activeResultTab === 'spa'
                  ? 'border-indigo-600 text-indigo-700'
                  : 'border-transparent text-slate-600 hover:text-slate-900'
              "
              @click="activeResultTab = 'spa'"
            >
              {{ t("explore.resTabSpa") }}
              <span class="ml-1 rounded-full bg-slate-200/80 px-1.5 py-0.5 text-[10px]">
                {{ result.spa_routes.length + result.micro_frontends.length }}
              </span>
            </button>
            <button
              type="button"
              class="border-b-2 px-3 py-2.5 text-xs font-medium transition"
              :class="
                activeResultTab === 'assets'
                  ? 'border-indigo-600 text-indigo-700'
                  : 'border-transparent text-slate-600 hover:text-slate-900'
              "
              @click="activeResultTab = 'assets'"
            >
              {{ t("explore.resTabAssets") }}
              <span class="ml-1 rounded-full bg-slate-200/80 px-1.5 py-0.5 text-[10px]">
                {{ result.subdomains.length + result.live_hosts.length }}
              </span>
            </button>
            <button
              type="button"
              class="border-b-2 px-3 py-2.5 text-xs font-medium transition"
              :class="
                activeResultTab === 'warnings'
                  ? 'border-indigo-600 text-indigo-700'
                  : 'border-transparent text-slate-600 hover:text-slate-900'
              "
              @click="activeResultTab = 'warnings'"
            >
              {{ t("explore.resTabWarnings") }}
              <span
                v-if="result.warnings.length > 0"
                class="ml-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-800"
              >
                {{ result.warnings.length }}
              </span>
            </button>
          </div>

          <span v-if="activeResultTab === 'endpoints'" class="pb-2 text-xs font-medium text-indigo-700">
            {{
              t("explore.selectedBadge", {
                selected: selectedEndpointIds.size,
                total: endpoints.length,
              })
            }}
          </span>
        </div>

        <!-- Tab 1: Discovered Endpoints Table -->
        <div v-if="activeResultTab === 'endpoints'">
          <!-- Filters Bar -->
          <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-3">
            <div class="flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                class="rounded-full px-2.5 py-1 text-xs font-medium transition"
                :class="
                  sourceFilter === 'all'
                    ? 'bg-indigo-600 text-white'
                    : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                "
                @click="sourceFilter = 'all'"
              >
                {{ t("explore.sourceAll") }} ({{ endpoints.length }})
              </button>
              <button
                v-for="sc in sourceCounts"
                :key="sc.source"
                type="button"
                class="rounded-full px-2.5 py-1 font-mono text-[11px] font-medium ring-1 ring-inset transition"
                :class="
                  sourceFilter === sc.source
                    ? 'bg-indigo-600 text-white ring-indigo-600'
                    : sourceBadgeClass(sc.source)
                "
                @click="sourceFilter = sourceFilter === sc.source ? 'all' : sc.source"
              >
                {{ sc.source }} ({{ sc.count }})
              </button>
            </div>

            <div class="flex flex-wrap items-center gap-2">
              <div class="relative">
                <Search class="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
                <input
                  v-model="endpointSearch"
                  type="text"
                  :placeholder="t('explore.searchEndpoints')"
                  class="input-field py-1 pl-8 pr-3 text-xs"
                />
              </div>
              <button
                type="button"
                class="btn-secondary px-2.5 py-1 text-xs"
                @click="toggleAllFiltered"
              >
                {{ allFilteredSelected ? t("explore.clearSelection") : t("explore.selectAllFiltered") }}
              </button>
              <button
                v-if="selectedEndpointIds.size > 0"
                type="button"
                class="btn-secondary px-2.5 py-1 text-xs"
                @click="clearSelection"
              >
                {{ t("explore.clearSelection") }}
              </button>
            </div>
          </div>

          <div v-if="filteredEndpoints.length === 0" class="px-5 py-12 text-center text-xs text-slate-500">
            {{ endpoints.length === 0 ? t("explore.noEndpointsFound") : t("explore.noEndpointsMatch") }}
          </div>

          <div v-else class="max-h-[460px] overflow-auto">
            <table class="min-w-full divide-y divide-slate-200 text-xs">
              <thead class="table-head sticky top-0 z-10">
                <tr>
                  <th class="w-10 px-4 py-2.5">
                    <input
                      type="checkbox"
                      class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                      :checked="allFilteredSelected"
                      @change="toggleAllFiltered"
                    />
                  </th>
                  <th class="w-8 px-1 py-2.5" />
                  <th class="w-20 px-3 py-2.5">{{ t("explore.colMethod") }}</th>
                  <th class="px-3 py-2.5">{{ t("explore.colPath") }}</th>
                  <th class="px-3 py-2.5">{{ t("explore.colSource") }}</th>
                  <th class="px-4 py-2.5">{{ t("explore.colParams") }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100 bg-white">
                <template v-for="ep in filteredEndpoints" :key="ep.id">
                  <tr class="hover:bg-slate-50/80">
                    <td class="px-4 py-2.5">
                      <input
                        type="checkbox"
                        class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                        :checked="selectedEndpointIds.has(ep.id)"
                        @change="toggleEndpointSelection(ep.id)"
                      />
                    </td>
                    <td class="px-1 py-2.5 text-slate-400">
                      <button
                        v-if="ep.snippet"
                        type="button"
                        class="rounded p-0.5 hover:bg-slate-100 hover:text-slate-700"
                        @click="toggleExpandEndpoint(ep.id)"
                      >
                        <ChevronDown v-if="expandedEndpointIds.has(ep.id)" class="h-3.5 w-3.5" />
                        <ChevronRight v-else class="h-3.5 w-3.5" />
                      </button>
                    </td>
                    <td class="whitespace-nowrap px-3 py-2.5">
                      <span
                        class="rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ring-1 ring-inset"
                        :class="methodBadgeClass(ep.method)"
                      >
                        {{ ep.method }}
                      </span>
                    </td>
                    <td class="px-3 py-2.5">
                      <p class="font-mono font-medium text-slate-800">{{ ep.path }}</p>
                      <p v-if="ep.url" class="mt-0.5 max-w-lg truncate font-mono text-[11px] text-slate-400">
                        {{ ep.url }}
                      </p>
                    </td>
                    <td class="whitespace-nowrap px-3 py-2.5">
                      <span
                        class="inline-flex rounded-full px-2 py-0.5 font-mono text-[11px] font-medium ring-1 ring-inset"
                        :class="sourceBadgeClass(ep.source)"
                      >
                        {{ ep.source }}
                      </span>
                    </td>
                    <td class="px-4 py-2.5">
                      <div v-if="(ep.inferred_params || []).length > 0" class="flex flex-wrap gap-1">
                        <span
                          v-for="p in ep.inferred_params.slice(0, 6)"
                          :key="p"
                          class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-700"
                        >
                          {{ p }}
                        </span>
                      </div>
                      <span v-else class="text-slate-400">—</span>
                    </td>
                  </tr>
                  <tr v-if="ep.snippet && expandedEndpointIds.has(ep.id)" class="bg-slate-50/70">
                    <td colspan="6" class="px-6 py-3">
                      <p class="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                        {{ t("explore.snippetLabel") }}
                      </p>
                      <pre
                        class="max-h-40 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200"
                      >{{ ep.snippet }}</pre>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Tab 2: SPA & Micro-Frontends -->
        <div v-else-if="activeResultTab === 'spa'" class="grid grid-cols-1 gap-5 p-5 lg:grid-cols-2">
          <div class="rounded-xl border border-slate-200 bg-slate-50/50 p-4">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-600">
              {{ t("explore.spaRoutesTitle", { count: result.spa_routes.length }) }}
            </h3>
            <p v-if="result.spa_routes.length === 0" class="mt-3 text-xs text-slate-500">
              {{ t("explore.spaRoutesEmpty") }}
            </p>
            <div v-else class="mt-3 flex flex-wrap gap-1.5">
              <span
                v-for="rt in result.spa_routes"
                :key="rt"
                class="rounded-md bg-white px-2 py-1 font-mono text-xs text-slate-700 ring-1 ring-slate-200"
              >
                {{ rt }}
              </span>
            </div>
          </div>

          <div class="rounded-xl border border-slate-200 bg-slate-50/50 p-4">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-600">
              {{ t("explore.microFrontendsTitle", { count: result.micro_frontends.length }) }}
            </h3>
            <p v-if="result.micro_frontends.length === 0" class="mt-3 text-xs text-slate-500">
              {{ t("explore.microFrontendsEmpty") }}
            </p>
            <ul v-else class="mt-3 space-y-1.5 font-mono text-xs text-slate-700">
              <li
                v-for="(mf, idx) in result.micro_frontends"
                :key="idx"
                class="rounded-md bg-white px-3 py-2 ring-1 ring-slate-200"
              >
                {{ formatAssetEntry(mf) }}
              </li>
            </ul>
          </div>
        </div>

        <!-- Tab 3: Subdomains & Live Hosts -->
        <div v-else-if="activeResultTab === 'assets'" class="grid grid-cols-1 gap-5 p-5 lg:grid-cols-2">
          <div class="rounded-xl border border-slate-200 bg-slate-50/50 p-4">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-600">
              {{ t("explore.subdomainsTitle", { count: result.subdomains.length }) }}
            </h3>
            <p v-if="result.subdomains.length === 0" class="mt-3 text-xs text-slate-500">
              {{ t("explore.subdomainsEmpty") }}
            </p>
            <div v-else class="mt-3 flex flex-wrap gap-1.5">
              <span
                v-for="sub in result.subdomains"
                :key="sub"
                class="rounded-md bg-white px-2 py-1 font-mono text-xs text-slate-700 ring-1 ring-slate-200"
              >
                {{ sub }}
              </span>
            </div>
          </div>

          <div class="rounded-xl border border-slate-200 bg-slate-50/50 p-4">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-600">
              {{ t("explore.liveHostsTitle", { count: result.live_hosts.length }) }}
            </h3>
            <p v-if="result.live_hosts.length === 0" class="mt-3 text-xs text-slate-500">
              {{ t("explore.liveHostsEmpty") }}
            </p>
            <ul v-else class="mt-3 space-y-1.5 font-mono text-xs text-slate-700">
              <li
                v-for="(host, idx) in result.live_hosts"
                :key="idx"
                class="rounded-md bg-white px-3 py-2 ring-1 ring-slate-200"
              >
                {{ formatAssetEntry(host) }}
              </li>
            </ul>
          </div>
        </div>

        <!-- Tab 4: Warnings -->
        <div v-else class="p-5">
          <p v-if="result.warnings.length === 0" class="py-6 text-center text-xs text-slate-500">
            {{ t("explore.warningsEmpty") }}
          </p>
          <ul v-else class="space-y-2">
            <li
              v-for="(warn, idx) in result.warnings"
              :key="idx"
              class="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50/70 px-3.5 py-2.5 text-xs text-amber-900"
            >
              <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              <span>{{ warn }}</span>
            </li>
          </ul>
        </div>
      </div>

      <!-- Sticky / Prominent Promote to Test Run Action Bar -->
      <div
        class="sticky bottom-3 z-20 rounded-xl border border-indigo-200 bg-white/95 p-4 shadow-lg ring-1 ring-indigo-500/10 backdrop-blur"
      >
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div class="space-y-2">
            <div class="flex items-center gap-2">
              <Layers class="h-4 w-4 text-indigo-600" />
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("explore.promoteTitle") }}
              </h3>
              <span class="rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700">
                {{
                  t("explore.selectedBadge", {
                    selected: selectedEndpointIds.size,
                    total: endpoints.length,
                  })
                }}
              </span>
            </div>
            <p class="text-xs text-slate-500">{{ t("explore.promoteDesc") }}</p>

            <div class="flex flex-wrap items-center gap-3 pt-1 text-xs">
              <div class="flex items-center gap-1.5">
                <span class="font-medium text-slate-600">{{ t("explore.promoteProjectMode") }}:</span>
                <select v-model="promoteProjectSelection" class="input-field w-auto py-1 text-xs">
                  <option value="__auto__">{{ t("explore.promoteAutoProject") }}</option>
                  <option value="__new__">{{ t("explore.promoteNewProject") }}</option>
                  <option v-for="proj in projects" :key="proj.id" :value="proj.id">
                    {{ proj.name }}
                  </option>
                </select>
                <input
                  v-if="promoteProjectSelection === '__new__'"
                  v-model="customProjectName"
                  type="text"
                  class="input-field w-44 py-1 text-xs"
                  :placeholder="t('explore.promoteNewProjectPlaceholder')"
                />
              </div>

              <div class="flex flex-wrap items-center gap-3">
                <span class="font-medium text-slate-600">{{ t("explore.promoteCapsLabel") }}:</span>
                <label class="inline-flex cursor-pointer items-center gap-1.5">
                  <input
                    v-model="promoteJava"
                    type="checkbox"
                    class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                  />
                  <span>{{ t("explore.capJava") }}</span>
                </label>
                <label class="inline-flex cursor-pointer items-center gap-1.5">
                  <input
                    v-model="promotePerf"
                    type="checkbox"
                    class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                  />
                  <span>{{ t("explore.capPerf") }}</span>
                </label>
                <label class="inline-flex cursor-pointer items-center gap-1.5">
                  <input
                    v-model="promoteSecurity"
                    type="checkbox"
                    class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                  />
                  <span>{{ t("explore.capSecurity") }}</span>
                </label>
              </div>
            </div>
          </div>

          <button
            type="button"
            data-testid="explore-promote-btn"
            class="btn-primary shrink-0 px-5 py-2.5 text-sm"
            :disabled="promoting"
            @click="handlePromoteToRun"
          >
            <Loader2 v-if="promoting" class="h-4 w-4 animate-spin" />
            <ArrowRight v-else class="h-4 w-4" />
            <span>{{ promoting ? t("explore.promotingBtn") : t("explore.promoteBtn") }}</span>
          </button>
        </div>
      </div>
    </template>
  </div>
</template>
