<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ClipboardPaste,
  Clock,
  Compass,
  Eye,
  EyeOff,
  FileCode2,
  GitMerge,
  Globe,
  History,
  KeyRound,
  Layers,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Terminal,
  Trash2,
  X,
} from "lucide-vue-next";
import {
  deleteExplore,
  getExplore,
  getExploreJob,
  listExploreJobs,
  listExplores,
  promoteExploreToRun,
  startExploreJob,
  verifyExploreAuth,
  type DiscoveredEndpoint,
  type ExploreAuthConfig,
  type ExploreAuthType,
  type ExploreJob,
  type ExploreListItem,
  type ExploreMode,
  type ExploreResult,
  type ExploreVerifyAuthResponse,
} from "@/api/explore";
import {
  discoverProjectPortals,
  listAuthProfiles,
  listProjects,
  listTargets,
} from "@/api/projects";
import type { AuthProfile, DiscoveredPortal, Project, Target } from "@/api/types";
import { parseAuthPaste } from "@/utils/authPasteParser";
import { useWorkspace } from "@/stores/workspace";
import EmptyState from "@/components/layout/EmptyState.vue";
import MetricCard from "@/components/layout/MetricCard.vue";
import AcceptancePromptModal from "@/components/workspace/AcceptancePromptModal.vue";
import DiscoveredPortalsPanel from "@/components/workspace/DiscoveredPortalsPanel.vue";
import { useI18n } from "@/i18n";
import "@/i18n/locales/explore";

const SAMPLE_CURL = `curl 'https://api.example.com/api/v1/orders/1001' \\
  -H 'Accept: application/json' \\
  -H 'Authorization: Bearer demo-session-token' \\
  -H 'X-Client-Platform: web'`;

const AUTH_HEADER_PATTERN = /^(authorization|cookie|x-auth-token|x-api-key|x-token|token)$/i;

const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const { activeProjectId, ensureLoaded, setActiveProject, workspacePath } = useWorkspace();

const projectId = computed(
  () =>
    (typeof route.params.projectId === "string" && route.params.projectId) ||
    activeProjectId.value ||
    ""
);

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
const runningPortals = ref(false);
const loadingHistory = ref(false);
const deleting = ref(false);
const confirmingDelete = ref(false);
const promoting = ref(false);
const error = ref<string | null>(null);
const historyItems = ref<ExploreListItem[]>([]);
const selectedHistoryId = ref("");
const result = ref<ExploreResult | null>(null);

// Async Explore Job Progress state
const PROGRESS_STAGES = [
  "init",
  "probe_openapi",
  "crawl_js",
  "discover_portals",
  "finalize",
] as const;

const activeJob = ref<ExploreJob | null>(null);
const elapsedSeconds = ref(0);
let pollTimer: number | null = null;
let elapsedTimer: number | null = null;
let completionHideTimer: number | null = null;
let pollSeq = 0;

const activeJobPct = computed(() => {
  const job = activeJob.value;
  if (!job) return 0;
  if (job.status === "completed") return 100;
  return Math.max(0, Math.min(100, Math.round(Number(job.progress_pct ?? 0))));
});

const activeStageIndex = computed(() => {
  const job = activeJob.value;
  if (!job) return -1;
  if (job.status === "completed" || job.stage === "completed") {
    return PROGRESS_STAGES.length;
  }
  const normalized = job.stage === "merge" ? "finalize" : job.stage;
  const idx = PROGRESS_STAGES.indexOf(normalized as (typeof PROGRESS_STAGES)[number]);
  if (idx >= 0) return idx;
  if (job.progress_pct >= 88) return 4;
  if (job.progress_pct >= 68) return 3;
  if (job.progress_pct >= 42) return 2;
  if (job.progress_pct >= 20) return 1;
  return 0;
});

// Project Multi-Portal Targets state
const projectTargets = ref<Target[]>([]);
const selectedPortalIds = ref<Set<string>>(new Set());
const discoveringPortals = ref(false);
const discoveredPortals = ref<DiscoveredPortal[] | null>(null);
const discoveredRootDomain = ref("");
const portalNotice = ref<string | null>(null);
const harPromptOpen = ref(false);

// Explore Auth Credentials state
const AUTH_MODE_GROUPS: ReadonlyArray<{
  groupKey: string;
  modes: ReadonlyArray<ExploreAuthType>;
}> = [
  { groupKey: "groupBase", modes: ["none", "profile"] },
  { groupKey: "groupStatic", modes: ["bearer", "cookie", "headers"] },
  { groupKey: "groupDynamic", modes: ["basic", "login_api", "form_login"] },
];
const ROLE_TAG_SUGGESTIONS = ["account_a", "account_b", "admin", "doctor", "pharmacy", "guest"] as const;

const projectAuthProfiles = ref<AuthProfile[]>([]);
const authPanelOpen = ref(true);
const authMode = ref<ExploreAuthType>("none");
const selectedAuthProfileId = ref("");
const bearerHeader = ref("Authorization");
const bearerPrefix = ref("Bearer ");
const bearerToken = ref("");
const showBearerToken = ref(false);
const cookieValue = ref("");
const showCookie = ref(false);
const basicUsername = ref("");
const basicPassword = ref("");
const showBasicPassword = ref(false);
const loginApiUrl = ref("");
const loginApiMethod = ref<"POST" | "PUT" | "PATCH">("POST");
const loginApiContentType = ref<"json" | "form">("json");
const loginApiUsernameField = ref("username");
const loginApiPasswordField = ref("password");
const loginApiUsername = ref("");
const loginApiPassword = ref("");
const showLoginApiPassword = ref(false);
const loginApiTokenJsonpath = ref("");
const loginApiTokenHeader = ref("Authorization");
const loginApiTokenPrefix = ref("Bearer ");
const loginApiExtraFields = ref<Record<string, unknown> | undefined>(undefined);
const formLoginUrl = ref("");
const formLoginUsername = ref("");
const formLoginPassword = ref("");
const showFormLoginPassword = ref(false);
const showFormSelectors = ref(false);
const formUsernameSelector = ref("");
const formPasswordSelector = ref("");
const formSubmitSelector = ref("");
const customHeaderRows = ref<Array<{ key: string; value: string }>>([{ key: "", value: "" }]);
const showCustomHeaderValues = ref(false);
const saveAsProfile = ref(false);
const saveProfileName = ref("");
const saveProfileRoleTag = ref("account_a");
const authSuccessNotice = ref<string | null>(null);
let lastNotifiedSavedProfileId = "";

// Burp-style Smart Paste state
const smartPasteOpen = ref(false);
const smartPasteText = ref("");
const smartPasteNotice = ref<string | null>(null);
const smartPasteError = ref<string | null>(null);

// Burp-style Pre-flight Session Validity Check state
const verifyUrl = ref("");
const verifyingAuth = ref(false);
const verifyResult = ref<ExploreVerifyAuthResponse | null>(null);
const verifyError = ref<string | null>(null);
let verifySeq = 0;

function resetInlineAuthState(): void {
  verifySeq += 1;
  authMode.value = "none";
  selectedAuthProfileId.value = "";
  bearerHeader.value = "Authorization";
  bearerPrefix.value = "Bearer ";
  bearerToken.value = "";
  showBearerToken.value = false;
  cookieValue.value = "";
  showCookie.value = false;
  basicUsername.value = "";
  basicPassword.value = "";
  showBasicPassword.value = false;
  loginApiUrl.value = "";
  loginApiMethod.value = "POST";
  loginApiContentType.value = "json";
  loginApiUsernameField.value = "username";
  loginApiPasswordField.value = "password";
  loginApiUsername.value = "";
  loginApiPassword.value = "";
  showLoginApiPassword.value = false;
  loginApiTokenJsonpath.value = "";
  loginApiTokenHeader.value = "Authorization";
  loginApiTokenPrefix.value = "Bearer ";
  loginApiExtraFields.value = undefined;
  formLoginUrl.value = "";
  formLoginUsername.value = "";
  formLoginPassword.value = "";
  showFormLoginPassword.value = false;
  showFormSelectors.value = false;
  formUsernameSelector.value = "";
  formPasswordSelector.value = "";
  formSubmitSelector.value = "";
  customHeaderRows.value = [{ key: "", value: "" }];
  showCustomHeaderValues.value = false;
  saveAsProfile.value = false;
  saveProfileName.value = "";
  saveProfileRoleTag.value = "account_a";
  authSuccessNotice.value = null;
  smartPasteOpen.value = false;
  smartPasteText.value = "";
  smartPasteNotice.value = null;
  smartPasteError.value = null;
  verifyUrl.value = "";
  verifyingAuth.value = false;
  verifyResult.value = null;
  verifyError.value = null;
}

watch(authMode, () => {
  verifySeq += 1;
  verifyingAuth.value = false;
  verifyResult.value = null;
  verifyError.value = null;
});

const activeAuthSummary = computed<string>(() => {
  if (authMode.value === "none") return "";
  if (authMode.value === "profile") {
    const prof = projectAuthProfiles.value.find((p) => p.id === selectedAuthProfileId.value);
    return prof ? `${prof.name} (${prof.role_tag})` : t("explore.auth.mode.profile");
  }
  return t(`explore.auth.mode.${authMode.value}`);
});

const resultAuthBadgeText = computed<string>(() => {
  const seed = result.value?.seed;
  if (!seed) return "";
  const mode = (seed.auth_mode || "").trim();
  const headers = seed.auth_headers_detected || [];
  if (mode === "profile" && seed.auth_profile_name) {
    return `profile · ${seed.auth_profile_name}`;
  }
  if (mode && mode !== "none") {
    return headers.length > 0 ? `${mode} (${headers.join(", ")})` : mode;
  }
  if (headers.length > 0) {
    return headers.join(", ");
  }
  return "";
});

function buildExploreAuthPayload(options?: { forVerify?: boolean }): {
  credential_id?: string | null;
  auth?: ExploreAuthConfig | null;
  error?: string;
} {
  const mode = authMode.value;
  if (mode === "none") {
    return {};
  }

  if (mode === "profile") {
    if (!selectedAuthProfileId.value) {
      return { error: t("explore.auth.errMissingProfile") };
    }
    return {
      credential_id: selectedAuthProfileId.value,
      auth: {
        auth_type: "profile",
        credential_id: selectedAuthProfileId.value,
        verify_url: verifyUrl.value.trim() || undefined,
      },
    };
  }

  const saveFields = options?.forVerify
    ? { save_as_profile: false }
    : projectId.value && saveAsProfile.value
      ? {
          save_as_profile: true,
          profile_name: saveProfileName.value.trim() || undefined,
          role_tag: saveProfileRoleTag.value.trim() || "account_a",
        }
      : {};
  const commonVerify = verifyUrl.value.trim() ? { verify_url: verifyUrl.value.trim() } : {};

  if (mode === "bearer") {
    const token = bearerToken.value.trim();
    if (!token) return { error: t("explore.auth.errMissingToken") };
    return {
      auth: {
        auth_type: "bearer",
        token,
        token_header: bearerHeader.value.trim() || "Authorization",
        token_prefix: bearerPrefix.value,
        ...commonVerify,
        ...saveFields,
      },
    };
  }

  if (mode === "cookie") {
    const cookie = cookieValue.value.trim();
    if (!cookie) return { error: t("explore.auth.errMissingCookie") };
    return {
      auth: {
        auth_type: "cookie",
        cookie,
        ...commonVerify,
        ...saveFields,
      },
    };
  }

  if (mode === "basic") {
    const username = basicUsername.value.trim();
    const password = basicPassword.value;
    if (!username || !password) return { error: t("explore.auth.errMissingBasic") };
    return {
      auth: {
        auth_type: "basic",
        username,
        password,
        ...commonVerify,
        ...saveFields,
      },
    };
  }

  if (mode === "login_api") {
    const url = loginApiUrl.value.trim();
    const username = loginApiUsername.value.trim();
    const password = loginApiPassword.value;
    if (!url || !username || !password) return { error: t("explore.auth.errMissingLoginApi") };
    return {
      auth: {
        auth_type: "login_api",
        login_url: url,
        login_method: loginApiMethod.value,
        login_content_type: loginApiContentType.value,
        username_field: loginApiUsernameField.value.trim() || "username",
        password_field: loginApiPasswordField.value.trim() || "password",
        username,
        password,
        extra_fields: loginApiExtraFields.value,
        token_jsonpath: loginApiTokenJsonpath.value.trim() || null,
        token_header: loginApiTokenHeader.value.trim() || "Authorization",
        token_prefix: loginApiTokenPrefix.value,
        ...commonVerify,
        ...saveFields,
      },
    };
  }

  if (mode === "form_login") {
    const url = formLoginUrl.value.trim();
    const username = formLoginUsername.value.trim();
    const password = formLoginPassword.value;
    if (!url || !username || !password) return { error: t("explore.auth.errMissingFormLogin") };
    return {
      auth: {
        auth_type: "form_login",
        login_url: url,
        username,
        password,
        username_selector: formUsernameSelector.value.trim() || null,
        password_selector: formPasswordSelector.value.trim() || null,
        submit_selector: formSubmitSelector.value.trim() || null,
        ...commonVerify,
        ...saveFields,
      },
    };
  }

  // headers
  const headers: Record<string, string> = {};
  for (const row of customHeaderRows.value) {
    const k = row.key.trim();
    const v = row.value.trim();
    if (k && v) headers[k] = v;
  }
  if (Object.keys(headers).length === 0) {
    return { error: t("explore.auth.errMissingHeaders") };
  }
  return {
    auth: {
      auth_type: "headers",
      headers,
      ...commonVerify,
      ...saveFields,
    },
  };
}

function normalizeLoginMethod(method: string | undefined): "POST" | "PUT" | "PATCH" {
  const upper = (method || "POST").toUpperCase();
  if (upper === "PUT" || upper === "PATCH") return upper;
  return "POST";
}

function handleSmartPasteParse(): void {
  smartPasteError.value = null;
  smartPasteNotice.value = null;
  const parsed = parseAuthPaste(smartPasteText.value);
  if (!parsed.ok) {
    smartPasteError.value =
      parsed.errorKey === "empty"
        ? t("explore.auth.smartPasteErrEmpty")
        : t("explore.auth.smartPasteErrNone");
    return;
  }

  authPanelOpen.value = true;
  authMode.value = parsed.mode;
  verifyResult.value = null;
  verifyError.value = null;

  if (parsed.mode === "login_api") {
    loginApiUrl.value = parsed.loginUrl ?? "";
    loginApiMethod.value = normalizeLoginMethod(parsed.loginMethod);
    loginApiContentType.value = parsed.loginContentType ?? "json";
    loginApiUsernameField.value = parsed.usernameField ?? "username";
    loginApiPasswordField.value = parsed.passwordField ?? "password";
    loginApiUsername.value = parsed.username ?? "";
    loginApiPassword.value = parsed.password ?? "";
    loginApiExtraFields.value = parsed.extraFields;
    smartPasteNotice.value = t("explore.auth.smartPasteExtractedLogin", {
      method: loginApiMethod.value,
      url: parsed.loginUrl || "",
    });
  } else {
    loginApiExtraFields.value = undefined;
    loginApiMethod.value = "POST";
    if (parsed.mode === "basic") {
      basicUsername.value = parsed.username ?? "";
      basicPassword.value = parsed.password ?? "";
      smartPasteNotice.value = t("explore.auth.smartPasteExtractedBasic", {
        user: parsed.username ?? "",
      });
    } else if (parsed.mode === "bearer") {
      bearerHeader.value = parsed.tokenHeader || "Authorization";
      bearerPrefix.value = parsed.tokenPrefix ?? "Bearer ";
      bearerToken.value = parsed.token ?? "";
      smartPasteNotice.value = t("explore.auth.smartPasteExtractedBearer", {
        header: bearerHeader.value,
      });
    } else if (parsed.mode === "cookie") {
      cookieValue.value = parsed.cookie ?? "";
      smartPasteNotice.value = t("explore.auth.smartPasteExtractedCookie");
    } else if (parsed.mode === "headers" && parsed.headers) {
      customHeaderRows.value = Object.entries(parsed.headers).map(([key, value]) => ({
        key,
        value,
      }));
      smartPasteNotice.value = t("explore.auth.smartPasteExtractedHeaders", {
        count: parsed.headerNames.length,
        headers: parsed.headerNames.join(", "),
      });
    }
  }

  if (parsed.mode !== "login_api" && parsed.detectedUrl && !verifyUrl.value.trim()) {
    verifyUrl.value = parsed.detectedUrl;
  }
  smartPasteText.value = "";
  smartPasteOpen.value = false;
}

async function handleVerifyAuth(): Promise<void> {
  if (verifyingAuth.value || authMode.value === "none") return;
  verifyError.value = null;
  verifyResult.value = null;

  const built = buildExploreAuthPayload({ forVerify: true });
  if (built.error) {
    verifyError.value = built.error;
    return;
  }

  let fallbackTarget = "";
  if (inputMode.value === "curl" && parsedSeedPreview.value.baseUrl) {
    fallbackTarget = parsedSeedPreview.value.baseUrl;
  } else if (inputMode.value === "url") {
    const firstLine = targetUrl.value
      .split(/[\r\n,]+/)
      .map((s) => s.trim())
      .find(Boolean);
    if (firstLine) fallbackTarget = firstLine;
  }
  if (!fallbackTarget && projectTargets.value.length > 0) {
    const firstSelected = projectTargets.value.find((tItem) =>
      selectedPortalIds.value.has(tItem.id)
    );
    fallbackTarget = (firstSelected ?? projectTargets.value[0]).base_url;
  }

  const mySeq = ++verifySeq;
  const pidSnapshot = projectId.value || null;
  verifyingAuth.value = true;
  try {
    const res = await verifyExploreAuth({
      verify_url: verifyUrl.value.trim() || null,
      target_url: fallbackTarget || null,
      project_id: pidSnapshot,
      credential_id: built.credential_id ?? null,
      auth: built.auth ?? null,
    });
    if (mySeq !== verifySeq || (projectId.value || null) !== pidSnapshot) return;
    verifyResult.value = res;
  } catch (err) {
    if (mySeq !== verifySeq || (projectId.value || null) !== pidSnapshot) return;
    verifyError.value = err instanceof Error ? err.message : t("explore.auth.verifyErrReq");
  } finally {
    if (mySeq === verifySeq && (projectId.value || null) === pidSnapshot) {
      verifyingAuth.value = false;
    }
  }
}

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

const resultMergedHosts = computed<string[]>(() => {
  const res = result.value;
  if (!res) return [];
  const hosts = new Set<string>();
  for (const h of res.merged_hosts ?? []) {
    if (h) hosts.add(h);
  }
  for (const h of res.seed_hosts ?? []) {
    if (h) hosts.add(h);
  }
  for (const et of res.explored_targets ?? []) {
    if (et.base_url) {
      try {
        hosts.add(new URL(et.base_url).host);
      } catch {
        hosts.add(et.base_url);
      }
    }
  }
  if (hosts.size === 0 && res.seed?.base_url) {
    try {
      hosts.add(new URL(res.seed.base_url).host);
    } catch {
      hosts.add(res.seed.base_url);
    }
  }
  return Array.from(hosts);
});

function portalTargetLabel(target: Target): string {
  const raw = target.scope_rules?.portal_label;
  return typeof raw === "string" ? raw.trim() : "";
}

function portalTargetHost(target: Target): string {
  try {
    return new URL(target.base_url).host;
  } catch {
    return target.root_domain || target.base_url;
  }
}

function togglePortalSelection(id: string): void {
  const next = new Set(selectedPortalIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selectedPortalIds.value = next;
}

function selectAllPortals(): void {
  selectedPortalIds.value = new Set(projectTargets.value.map((tItem) => tItem.id));
}

function clearPortalSelection(): void {
  selectedPortalIds.value = new Set();
}

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
    item.root_domain ||
    item.seed?.base_url ||
    item.base_url ||
    "target";
  const count = item.summary?.endpoints_count ?? item.endpoints_count ?? 0;
  const mergedCount = (item.merged_hosts ?? item.seed_hosts ?? []).length;
  const hostSuffix =
    mergedCount > 1 ? ` · ${t("explore.historyHostsBadge", { count: mergedCount })}` : "";
  const scans = item.scans_count ?? item.summary?.scans_count;
  if (typeof scans === "number" && scans > 0) {
    if (scans === 1) {
      return `${t("explore.historyOptionSingleScanFmt", { domain, count })}${hostSuffix}`;
    }
    return `${t("explore.historyOptionScansFmt", { domain, count, scans })}${hostSuffix}`;
  }
  const timeStr = formatShortTime(item.updated_at || item.created_at);
  if (timeStr) {
    return `${t("explore.historyOptionFmt", { domain, count, time: timeStr })}${hostSuffix}`;
  }
  return `${t("explore.historyOptionNoTime", { domain, count })}${hostSuffix}`;
}

function startNewExplore(): void {
  selectedHistoryId.value = "";
  result.value = null;
  error.value = null;
  confirmingDelete.value = false;
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
  if (Array.isArray(res.discovered_portals) && res.discovered_portals.length > 0) {
    discoveredPortals.value = res.discovered_portals;
    discoveredRootDomain.value = res.seed?.root_domain || "";
  }
  if (res.saved_auth_profile) {
    const saved = res.saved_auth_profile;
    const exists = projectAuthProfiles.value.some((p) => p.id === saved.id);
    projectAuthProfiles.value = exists
      ? projectAuthProfiles.value.map((p) => (p.id === saved.id ? saved : p))
      : [...projectAuthProfiles.value, saved];
    if (projectId.value) {
      void listAuthProfiles(projectId.value)
        .then((fresh) => {
          projectAuthProfiles.value = fresh;
        })
        .catch(() => {
          // keep optimistic update
        });
    }
    if (lastNotifiedSavedProfileId !== saved.id) {
      lastNotifiedSavedProfileId = saved.id;
      const toastMsg = t("explore.auth.savedProfileToast", {
        name: saved.name,
        role: saved.role_tag,
      });
      authSuccessNotice.value = toastMsg;
      window.setTimeout(() => {
        if (authSuccessNotice.value === toastMsg) authSuccessNotice.value = null;
      }, 6000);
    }
  }
}

async function handleDiscoverSiblingPortals(): Promise<void> {
  if (!projectId.value || discoveringPortals.value) return;
  discoveringPortals.value = true;
  error.value = null;
  portalNotice.value = null;
  try {
    const res = await discoverProjectPortals(projectId.value);
    discoveredPortals.value = res.portals ?? [];
    discoveredRootDomain.value = res.root_domain || "";
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("settings.target.errDiscover");
  } finally {
    discoveringPortals.value = false;
  }
}

async function onBatchPortalsAdded(added: Target[], skipped: number): Promise<void> {
  portalNotice.value = t("settings.target.batchAddedToast", {
    added: added.length,
    skipped,
  });
  if (projectId.value) {
    try {
      const fresh = await listTargets(projectId.value);
      projectTargets.value = fresh;
      const nextSel = new Set(selectedPortalIds.value);
      for (const tItem of added) nextSel.add(tItem.id);
      selectedPortalIds.value = nextSel;
    } catch {
      // ignore refresh failure
    }
  }
  window.setTimeout(() => {
    portalNotice.value = null;
  }, 5000);
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

async function fetchHistoryAndProjects(resetSelection = false): Promise<void> {
  loadingHistory.value = true;
  if (resetSelection) {
    result.value = null;
    selectedHistoryId.value = "";
    confirmingDelete.value = false;
  }
  const pid = projectId.value || null;
  const [histRes, projRes, targetsRes, profilesRes] = await Promise.allSettled([
    listExplores(pid),
    listProjects(),
    pid ? listTargets(pid) : Promise.resolve([] as Target[]),
    pid ? listAuthProfiles(pid) : Promise.resolve([] as AuthProfile[]),
  ]);
  if ((projectId.value || null) !== pid) return;
  if (histRes.status === "fulfilled") {
    historyItems.value = histRes.value;
    if (!result.value && histRes.value.length > 0) {
      const firstId = histRes.value[0].explore_id;
      try {
        const full = await getExplore(firstId);
        if ((projectId.value || null) !== pid) return;
        applyResult(full);
      } catch {
        // Ignore initial history auto-load failure
      }
    }
  }
  if (projRes.status === "fulfilled") {
    projects.value = projRes.value;
    if (projectId.value && projRes.value.some((p) => p.id === projectId.value)) {
      promoteProjectSelection.value = projectId.value;
    }
  }
  if (targetsRes.status === "fulfilled") {
    projectTargets.value = targetsRes.value;
    selectedPortalIds.value = new Set(targetsRes.value.map((tItem) => tItem.id));
  } else {
    projectTargets.value = [];
    selectedPortalIds.value = new Set();
  }
  if (profilesRes.status === "fulfilled") {
    projectAuthProfiles.value = profilesRes.value;
    if (
      selectedAuthProfileId.value &&
      !profilesRes.value.some((p) => p.id === selectedAuthProfileId.value)
    ) {
      selectedAuthProfileId.value = "";
    }
  } else {
    projectAuthProfiles.value = [];
    selectedAuthProfileId.value = "";
  }
  loadingHistory.value = false;
}

async function onSelectHistory(exploreId: string): Promise<void> {
  confirmingDelete.value = false;
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

async function handleDeleteExplore(): Promise<void> {
  const targetId = selectedHistoryId.value || result.value?.explore_id;
  if (!targetId || deleting.value) return;
  deleting.value = true;
  error.value = null;
  try {
    await deleteExplore(targetId);
    confirmingDelete.value = false;
    const updatedList = await listExplores(projectId.value || null).catch(() =>
      historyItems.value.filter((item) => item.explore_id !== targetId)
    );
    historyItems.value = updatedList;
    if (updatedList.length > 0) {
      const nextId = updatedList[0].explore_id;
      try {
        const full = await getExplore(nextId);
        applyResult(full);
      } catch {
        startNewExplore();
      }
    } else {
      startNewExplore();
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("explore.errorDelete");
  } finally {
    deleting.value = false;
  }
}

function stopJobPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
  if (elapsedTimer !== null) {
    window.clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function clearCompletionHideTimer(): void {
  if (completionHideTimer !== null) {
    window.clearTimeout(completionHideTimer);
    completionHideTimer = null;
  }
}

function syncElapsedSeconds(job: ExploreJob | null): void {
  if (!job?.started_at) {
    elapsedSeconds.value = 0;
    return;
  }
  const startMs = Date.parse(job.started_at);
  if (Number.isNaN(startMs)) {
    elapsedSeconds.value = 0;
    return;
  }
  const endMs =
    job.finished_at && !Number.isNaN(Date.parse(job.finished_at))
      ? Date.parse(job.finished_at)
      : Date.now();
  elapsedSeconds.value = Math.max(0, Math.floor((endMs - startMs) / 1000));
}

function dismissProgressCard(): void {
  clearCompletionHideTimer();
  if (activeJob.value?.status !== "running") {
    activeJob.value = null;
  }
}

async function finalizeCompletedJob(job: ExploreJob, pidSnapshot: string | null): Promise<void> {
  if ((projectId.value || null) !== pidSnapshot) return;
  let finalResult = job.result ?? null;
  if (!finalResult && job.explore_id) {
    try {
      finalResult = await getExplore(job.explore_id);
    } catch {
      // Ignore fallback fetch error; history refresh will still surface it
    }
  }
  if ((projectId.value || null) !== pidSnapshot) return;
  if (finalResult) {
    applyResult(finalResult);
  }
  const updatedList = await listExplores(pidSnapshot).catch(() => historyItems.value);
  if ((projectId.value || null) !== pidSnapshot) return;
  historyItems.value = updatedList;
  if (pidSnapshot && (finalResult?.saved_auth_profile || finalResult?.seed?.auth_profile_id)) {
    const freshProfiles = await listAuthProfiles(pidSnapshot).catch(() => projectAuthProfiles.value);
    if ((projectId.value || null) === pidSnapshot) {
      projectAuthProfiles.value = freshProfiles;
    }
  }

  clearCompletionHideTimer();
  completionHideTimer = window.setTimeout(() => {
    if (activeJob.value?.job_id === job.job_id && activeJob.value.status === "completed") {
      activeJob.value = null;
    }
  }, 6000);
}

function attachAndPollJob(job: ExploreJob, isPortalsJob = false): void {
  stopJobPolling();
  clearCompletionHideTimer();
  const currentPollSeq = ++pollSeq;
  const pidSnapshot = projectId.value || null;

  activeJob.value = job;
  syncElapsedSeconds(job);

  if (job.status === "completed") {
    running.value = false;
    runningPortals.value = false;
    void finalizeCompletedJob(job, pidSnapshot);
    return;
  }
  if (job.status === "failed") {
    running.value = false;
    runningPortals.value = false;
    error.value = job.error || t("explore.errorRun");
    return;
  }

  running.value = !isPortalsJob;
  runningPortals.value = isPortalsJob;

  elapsedTimer = window.setInterval(() => {
    if (activeJob.value?.status === "running") {
      syncElapsedSeconds(activeJob.value);
    }
  }, 1000);

  let inFlight = false;
  pollTimer = window.setInterval(async () => {
    if (inFlight || currentPollSeq !== pollSeq) return;
    inFlight = true;
    try {
      const updated = await getExploreJob(job.job_id);
      if (currentPollSeq !== pollSeq || (projectId.value || null) !== pidSnapshot) return;
      activeJob.value = updated;
      syncElapsedSeconds(updated);
      if (updated.status === "completed") {
        stopJobPolling();
        running.value = false;
        runningPortals.value = false;
        await finalizeCompletedJob(updated, pidSnapshot);
      } else if (updated.status === "failed") {
        stopJobPolling();
        running.value = false;
        runningPortals.value = false;
        error.value = updated.error || t("explore.errorRun");
      }
    } catch {
      // Transient poll error; keep polling next tick
    } finally {
      inFlight = false;
    }
  }, 800);
}

async function checkAndResumeActiveJob(): Promise<void> {
  const pidSnapshot = projectId.value || null;
  try {
    const jobs = await listExploreJobs(pidSnapshot, true);
    if ((projectId.value || null) !== pidSnapshot) return;
    const runningJob = jobs.find((j) => j.status === "running");
    if (runningJob && activeJob.value?.job_id !== runningJob.job_id) {
      attachAndPollJob(runningJob, runningJob.targets_total > 1);
    }
  } catch {
    // Ignore resume probe failure
  }
}

async function handleRunExplore(): Promise<void> {
  if (running.value || runningPortals.value) return;
  error.value = null;
  const curlText = inputMode.value === "curl" ? seedCurl.value.trim() : "";
  const urlLines =
    inputMode.value === "url"
      ? targetUrl.value
          .split(/[\r\n,]+/)
          .map((s) => s.trim())
          .filter(Boolean)
      : [];

  if (!curlText && urlLines.length === 0) {
    error.value = t("explore.validationMissingInput");
    return;
  }

  const authPayload = buildExploreAuthPayload();
  if (authPayload.error) {
    error.value = authPayload.error;
    return;
  }

  running.value = true;
  try {
    const job = await startExploreJob({
      project_id: projectId.value || null,
      seed_curl: curlText || null,
      target_url: urlLines.length === 1 ? urlLines[0] : null,
      target_urls: urlLines.length > 1 ? urlLines : null,
      mode: discoveryMode.value,
      probe_openapi_docs: probeOpenApiDocs.value,
      crawl_js: crawlJs.value,
      depth: depth.value,
      credential_id: authPayload.credential_id ?? null,
      auth: authPayload.auth ?? null,
    });
    attachAndPollJob(job, false);
  } catch (err) {
    running.value = false;
    error.value = err instanceof Error ? err.message : t("explore.errorRun");
  }
}

async function handleExploreSelectedPortals(): Promise<void> {
  if (!projectId.value || selectedPortalIds.value.size === 0 || runningPortals.value || running.value) {
    return;
  }
  error.value = null;

  const authPayload = buildExploreAuthPayload();
  if (authPayload.error) {
    error.value = authPayload.error;
    return;
  }

  runningPortals.value = true;
  try {
    const job = await startExploreJob({
      project_id: projectId.value,
      explore_all_targets: true,
      target_ids: Array.from(selectedPortalIds.value),
      mode: discoveryMode.value,
      probe_openapi_docs: probeOpenApiDocs.value,
      crawl_js: crawlJs.value,
      depth: depth.value,
      credential_id: authPayload.credential_id ?? null,
      auth: authPayload.auth ?? null,
    });
    attachAndPollJob(job, true);
  } catch (err) {
    runningPortals.value = false;
    error.value = err instanceof Error ? err.message : t("explore.errorRun");
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
    if (createdRun.project_id) {
      setActiveProject(createdRun.project_id);
      await router.push(`/p/${createdRun.project_id}/runs/${createdRun.id}`);
    } else {
      await router.push(`/runs/${createdRun.id}`);
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("explore.errorPromote");
  } finally {
    promoting.value = false;
  }
}

watch(projectId, () => {
  stopJobPolling();
  clearCompletionHideTimer();
  pollSeq += 1;
  activeJob.value = null;
  running.value = false;
  runningPortals.value = false;
  discoveredPortals.value = null;
  discoveredRootDomain.value = "";
  portalNotice.value = null;
  seedCurl.value = "";
  targetUrl.value = "";
  resetInlineAuthState();
  void fetchHistoryAndProjects(true);
  void checkAndResumeActiveJob();
});

onMounted(async () => {
  await ensureLoaded();
  void fetchHistoryAndProjects();
  void checkAndResumeActiveJob();
});

onBeforeUnmount(() => {
  stopJobPolling();
  clearCompletionHideTimer();
  pollSeq += 1;
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

      <div class="flex flex-wrap items-center gap-2.5">
        <button
          v-if="projectId"
          type="button"
          data-testid="explore-agent-har-prompt-btn"
          class="inline-flex items-center gap-1.5 rounded-xl border border-indigo-200 bg-indigo-50 px-3.5 py-2 text-xs font-semibold text-indigo-700 shadow-sm transition hover:border-indigo-300 hover:bg-indigo-100"
          @click="harPromptOpen = true"
        >
          <Bot class="h-4 w-4 text-indigo-600" />
          <span>{{ t("acceptPrompt.harExploreBtn") }}</span>
        </button>

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

        <template v-if="selectedHistoryId">
          <div
            v-if="confirmingDelete"
            class="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-rose-50 px-2 py-1 text-xs"
          >
            <span class="font-medium text-rose-800">{{ t("explore.deleteConfirmText") }}</span>
            <button
              type="button"
              data-testid="explore-delete-confirm"
              class="rounded bg-rose-600 px-2 py-0.5 font-medium text-white hover:bg-rose-700 disabled:opacity-50"
              :disabled="deleting"
              @click="handleDeleteExplore"
            >
              {{ deleting ? t("explore.deletingBtn") : t("explore.deleteConfirmBtn") }}
            </button>
            <button
              type="button"
              class="rounded bg-white px-2 py-0.5 font-medium text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50"
              :disabled="deleting"
              @click="confirmingDelete = false"
            >
              {{ t("explore.deleteCancelBtn") }}
            </button>
          </div>
          <button
            v-else
            type="button"
            data-testid="explore-delete-btn"
            class="btn-danger py-1 text-xs"
            :title="t('explore.deleteTitle')"
            :disabled="loadingHistory || deleting"
            @click="confirmingDelete = true"
          >
            <Trash2 class="h-3.5 w-3.5" />
            <span>{{ t("explore.deleteBtn") }}</span>
          </button>
        </template>

        <button
          type="button"
          class="btn-secondary py-1 text-xs"
          :disabled="loadingHistory"
          @click="fetchHistoryAndProjects()"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="loadingHistory ? 'animate-spin' : ''" />
          <span>{{ t("explore.refreshHistory") }}</span>
        </button>
        </div>
      </div>
    </div>

    <!-- Project Portals One-Click Multi-Portal Explore Card -->
    <div
      v-if="projectTargets.length > 0"
      data-testid="explore-project-portals"
      class="card overflow-hidden border-indigo-200/80 bg-gradient-to-r from-white to-indigo-50/40"
    >
      <div class="flex flex-wrap items-center justify-between gap-3 border-b border-indigo-100 px-5 py-3">
        <div>
          <div class="flex items-center gap-2">
            <Globe class="h-4 w-4 text-indigo-600" />
            <h2 class="text-sm font-semibold text-slate-900">{{ t("explore.portalsTitle") }}</h2>
            <span class="rounded-full bg-indigo-100 px-2 py-0.5 font-mono text-[11px] font-semibold text-indigo-700">
              {{ projectTargets.length }}
            </span>
            <span
              v-if="authMode !== 'none'"
              class="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 ring-1 ring-inset ring-emerald-600/20"
            >
              <KeyRound class="h-3 w-3" />
              <span>{{ t("explore.auth.portalsActiveBadge", { detail: activeAuthSummary }) }}</span>
            </span>
          </div>
          <p class="mt-0.5 text-xs text-slate-500">{{ t("explore.portalsSubtitle") }}</p>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="discover-portals-btn"
            class="btn-secondary !px-3 !py-1.5 !text-xs"
            :disabled="discoveringPortals || !projectId"
            @click="handleDiscoverSiblingPortals"
          >
            <Loader2 v-if="discoveringPortals" class="h-3.5 w-3.5 animate-spin text-emerald-600" />
            <Sparkles v-else class="h-3.5 w-3.5 text-emerald-600" />
            <span>
              {{
                discoveringPortals
                  ? t("explore.discoveringPortalsBtn")
                  : t("explore.discoverPortalsBtn")
              }}
            </span>
          </button>
          <button
            type="button"
            class="text-xs font-medium text-indigo-600 hover:underline"
            @click="selectAllPortals"
          >
            {{ t("explore.portalsSelectAll") }}
          </button>
          <span class="text-slate-300">·</span>
          <button
            type="button"
            class="text-xs font-medium text-slate-500 hover:underline"
            @click="clearPortalSelection"
          >
            {{ t("explore.portalsClear") }}
          </button>
          <button
            type="button"
            data-testid="explore-all-portals-btn"
            class="btn-primary !px-3.5 !py-1.5 !text-xs"
            :disabled="selectedPortalIds.size === 0 || runningPortals || running"
            @click="handleExploreSelectedPortals"
          >
            <Loader2 v-if="runningPortals" class="h-3.5 w-3.5 animate-spin" />
            <Compass v-else class="h-3.5 w-3.5" />
            <span>
              {{
                runningPortals
                  ? t("explore.portalsExploringBtn")
                  : t("explore.portalsExploreBtn", { count: selectedPortalIds.size })
              }}
            </span>
          </button>
        </div>
      </div>

      <div class="flex flex-wrap gap-2 px-5 py-3.5">
        <button
          v-for="tItem in projectTargets"
          :key="tItem.id"
          type="button"
          data-testid="explore-portal-pill"
          class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs transition"
          :class="
            selectedPortalIds.has(tItem.id)
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'bg-white text-slate-700 ring-1 ring-slate-200 hover:bg-slate-50'
          "
          @click="togglePortalSelection(tItem.id)"
        >
          <Check v-if="selectedPortalIds.has(tItem.id)" class="h-3.5 w-3.5 shrink-0" />
          <span v-if="portalTargetLabel(tItem)" class="font-semibold">
            {{ portalTargetLabel(tItem) }}
          </span>
          <span v-if="portalTargetLabel(tItem)" class="opacity-60">·</span>
          <span class="font-mono text-[11px]" :class="selectedPortalIds.has(tItem.id) ? 'text-indigo-100' : 'text-slate-500'">
            {{ portalTargetHost(tItem) }}
          </span>
        </button>
      </div>

      <div v-if="portalNotice" class="px-5 pb-3 text-xs font-medium text-emerald-700">
        {{ portalNotice }}
      </div>

      <div v-if="discoveredPortals !== null && projectId" class="px-5 pb-4">
        <DiscoveredPortalsPanel
          :project-id="projectId"
          :portals="discoveredPortals"
          :existing-targets="projectTargets"
          :root-domain="discoveredRootDomain"
          @added="onBatchPortalsAdded"
          @error="(msg) => (error = msg)"
        />
      </div>
    </div>

    <!-- Discovered Portals Panel when project has 0 targets yet but explore returned discovered_portals -->
    <DiscoveredPortalsPanel
      v-else-if="discoveredPortals !== null && discoveredPortals.length > 0 && projectId"
      :project-id="projectId"
      :portals="discoveredPortals"
      :existing-targets="projectTargets"
      :root-domain="discoveredRootDomain"
      @added="onBatchPortalsAdded"
      @error="(msg) => (error = msg)"
    />

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
        <!-- Auto-Merge by Root Domain Hint -->
        <div
          data-testid="explore-merge-hint"
          class="inline-flex items-center gap-1.5 rounded-lg border border-indigo-100 bg-indigo-50/50 px-3 py-1.5 text-[11px] font-medium text-indigo-800"
        >
          <GitMerge class="h-3.5 w-3.5 shrink-0 text-indigo-600" />
          <span>{{ t("explore.mergeHintBadge") }}</span>
        </div>

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

        <!-- Tab 2: Multi-Domain / URLs -->
        <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div class="sm:col-span-2">
            <label class="block text-xs font-medium text-slate-700">
              {{ t("explore.targetUrlLabel") }}
            </label>
            <p class="mt-0.5 text-[11px] text-slate-500">{{ t("explore.multiUrlHint") }}</p>
            <textarea
              v-model="targetUrl"
              data-testid="explore-multi-urls-input"
              rows="3"
              class="input-field mt-1.5 font-mono text-xs leading-relaxed"
              :placeholder="t('explore.multiUrlPlaceholder')"
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

        <!-- Collapsible Auth Credentials Section (Burp-style grouped modes + Smart Paste + Verify) -->
        <div
          data-testid="explore-auth-panel"
          class="overflow-hidden rounded-xl border border-slate-200 bg-slate-50/60"
        >
          <!-- Panel Header -->
          <div class="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
            <button
              type="button"
              data-testid="explore-auth-toggle"
              class="flex flex-wrap items-center gap-2 text-left text-xs font-semibold text-slate-800 hover:text-indigo-700"
              @click="authPanelOpen = !authPanelOpen"
            >
              <ChevronDown v-if="authPanelOpen" class="h-3.5 w-3.5 text-slate-400" />
              <ChevronRight v-else class="h-3.5 w-3.5 text-slate-400" />
              <KeyRound class="h-3.5 w-3.5 text-indigo-600" />
              <span>{{ t("explore.auth.panelTitle") }}</span>
              <span
                class="rounded-full px-2 py-0.5 text-[10px] font-medium ring-1 ring-inset"
                :class="
                  authMode === 'none'
                    ? 'bg-white text-slate-500 ring-slate-200'
                    : 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
                "
              >
                {{
                  authMode === "none"
                    ? t("explore.auth.panelSummaryNone")
                    : t("explore.auth.panelSummaryActive", { detail: activeAuthSummary })
                }}
              </span>
            </button>

            <div class="flex flex-wrap items-center gap-2">
              <span
                class="inline-flex items-center gap-1 rounded-full bg-white px-2.5 py-0.5 text-[10px] font-medium text-slate-500 ring-1 ring-inset ring-slate-200"
              >
                <ShieldCheck class="h-3 w-3 text-emerald-600" />
                <span>{{ t("explore.auth.securityBadge") }}</span>
              </span>
              <button
                type="button"
                data-testid="explore-smart-paste-toggle"
                class="inline-flex items-center gap-1 rounded-lg border border-indigo-200 bg-white px-2.5 py-1 text-[11px] font-medium text-indigo-700 transition hover:bg-indigo-50"
                @click="
                  smartPasteOpen = !smartPasteOpen;
                  if (smartPasteOpen) authPanelOpen = true;
                "
              >
                <ClipboardPaste class="h-3 w-3" />
                <span>{{ t("explore.auth.smartPasteBtn") }}</span>
              </button>
            </div>
          </div>

          <div v-if="authPanelOpen" class="space-y-3 border-t border-slate-200/80 bg-white px-4 py-3">
            <!-- Burp-Style Smart Paste Inline Box -->
            <div
              v-if="smartPasteOpen"
              class="space-y-2 rounded-lg border border-indigo-200 bg-indigo-50/40 p-3"
            >
              <div class="flex items-center justify-between gap-2">
                <span class="text-xs font-semibold text-indigo-900">
                  {{ t("explore.auth.smartPasteTitle") }}
                </span>
                <button
                  type="button"
                  class="text-slate-400 hover:text-slate-600"
                  @click="smartPasteOpen = false"
                >
                  <X class="h-3.5 w-3.5" />
                </button>
              </div>
              <textarea
                v-model="smartPasteText"
                data-testid="explore-smart-paste-input"
                rows="3"
                class="input-field bg-white font-mono text-[11px] leading-relaxed"
                :placeholder="t('explore.auth.smartPastePlaceholder')"
              />
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p v-if="smartPasteError" class="text-[11px] font-medium text-rose-600">
                  {{ smartPasteError }}
                </p>
                <span v-else class="text-[11px] text-slate-500">
                  {{ t("explore.auth.appliesToAllHint") }}
                </span>
                <button
                  type="button"
                  data-testid="explore-smart-paste-apply"
                  class="btn-primary !px-3 !py-1 !text-xs"
                  @click="handleSmartPasteParse"
                >
                  <Sparkles class="h-3 w-3" />
                  <span>{{ t("explore.auth.smartPasteApply") }}</span>
                </button>
              </div>
            </div>

            <!-- Smart Paste Extraction Success Summary -->
            <div
              v-if="smartPasteNotice"
              class="flex items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs text-emerald-800"
            >
              <span class="flex items-center gap-1.5">
                <CheckCircle2 class="h-3.5 w-3.5 shrink-0 text-emerald-600" />
                <span>{{ smartPasteNotice }}</span>
              </span>
              <button
                type="button"
                class="text-emerald-600 hover:text-emerald-800"
                @click="smartPasteNotice = null"
              >
                <X class="h-3.5 w-3.5" />
              </button>
            </div>

            <!-- Burp-Friendly Grouped Mode Pills -->
            <div class="flex flex-wrap items-center gap-y-2 gap-x-3">
              <div
                v-for="(grp, gIdx) in AUTH_MODE_GROUPS"
                :key="grp.groupKey"
                class="flex flex-wrap items-center gap-1.5"
                :class="gIdx > 0 ? 'border-l border-slate-200 pl-3' : ''"
              >
                <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
                  {{ t(`explore.auth.${grp.groupKey}`) }}
                </span>
                <button
                  v-for="mode in grp.modes"
                  :key="mode"
                  type="button"
                  :data-testid="`explore-auth-mode-${mode}`"
                  class="rounded-full px-2.5 py-1 text-xs font-medium transition"
                  :class="
                    authMode === mode
                      ? 'bg-indigo-600 text-white shadow-sm'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200 hover:text-slate-900'
                  "
                  @click="authMode = mode"
                >
                  {{ t(`explore.auth.mode.${mode}`) }}
                </button>
              </div>
            </div>

            <!-- Mode: Saved Project AuthProfile -->
            <div v-if="authMode === 'profile'" class="space-y-2 pt-1">
              <template v-if="!projectId">
                <p class="text-xs text-slate-500">{{ t("explore.auth.noProjectProfileHint") }}</p>
              </template>
              <template v-else-if="projectAuthProfiles.length === 0">
                <p class="text-xs text-slate-600">
                  {{ t("explore.auth.noProfilesHint") }}
                  <RouterLink
                    :to="`${workspacePath('settings', projectId)}?tab=auth`"
                    class="font-semibold text-indigo-600 hover:underline"
                  >
                    {{ t("explore.auth.manageProfilesLink") }} →
                  </RouterLink>
                </p>
              </template>
              <template v-else>
                <div class="flex flex-wrap items-end gap-3">
                  <div class="min-w-[260px] flex-1">
                    <label class="mb-1 block text-[11px] font-medium text-slate-700">
                      {{ t("explore.auth.selectProfileLabel") }}
                    </label>
                    <select
                      v-model="selectedAuthProfileId"
                      data-testid="explore-auth-profile-select"
                      class="input-field !py-1.5 text-xs"
                    >
                      <option value="">{{ t("explore.auth.selectProfilePlaceholder") }}</option>
                      <option
                        v-for="prof in projectAuthProfiles"
                        :key="prof.id"
                        :value="prof.id"
                      >
                        {{ prof.name }} ({{ prof.role_tag }} · {{ prof.auth_type || "headers" }})
                      </option>
                    </select>
                  </div>
                  <RouterLink
                    :to="`${workspacePath('settings', projectId)}?tab=auth`"
                    class="pb-1.5 text-xs font-medium text-indigo-600 hover:underline"
                  >
                    {{ t("explore.auth.manageProfilesLink") }} →
                  </RouterLink>
                </div>
              </template>
            </div>

            <!-- Mode: Bearer / Token -->
            <div v-else-if="authMode === 'bearer'" class="grid grid-cols-1 gap-3 pt-1 sm:grid-cols-12">
              <div class="sm:col-span-3">
                <label class="mb-1 block text-[11px] font-medium text-slate-700">
                  {{ t("explore.auth.tokenHeaderLabel") }}
                </label>
                <input
                  v-model="bearerHeader"
                  type="text"
                  placeholder="Authorization"
                  class="input-field !py-1.5 font-mono text-xs"
                />
              </div>
              <div class="sm:col-span-2">
                <label class="mb-1 block text-[11px] font-medium text-slate-700">
                  {{ t("explore.auth.tokenPrefixLabel") }}
                </label>
                <input
                  v-model="bearerPrefix"
                  type="text"
                  placeholder="Bearer "
                  class="input-field !py-1.5 font-mono text-xs"
                />
              </div>
              <div class="sm:col-span-7">
                <label class="mb-1 block text-[11px] font-medium text-slate-700">
                  {{ t("explore.auth.tokenValueLabel") }}
                </label>
                <div class="relative">
                  <input
                    v-model="bearerToken"
                    data-testid="explore-auth-token-input"
                    :type="showBearerToken ? 'text' : 'password'"
                    :placeholder="t('explore.auth.tokenValuePlaceholder')"
                    class="input-field !py-1.5 pr-8 font-mono text-xs"
                  />
                  <button
                    type="button"
                    class="absolute right-2 top-1.5 text-slate-400 hover:text-slate-600"
                    :title="showBearerToken ? t('explore.auth.hideSecret') : t('explore.auth.showSecret')"
                    @click="showBearerToken = !showBearerToken"
                  >
                    <EyeOff v-if="showBearerToken" class="h-3.5 w-3.5" />
                    <Eye v-else class="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </div>

            <!-- Mode: Cookie -->
            <div v-else-if="authMode === 'cookie'" class="pt-1">
              <label class="mb-1 block text-[11px] font-medium text-slate-700">
                {{ t("explore.auth.cookieLabel") }}
              </label>
              <div class="relative">
                <input
                  v-model="cookieValue"
                  data-testid="explore-auth-cookie-input"
                  :type="showCookie ? 'text' : 'password'"
                  :placeholder="t('explore.auth.cookiePlaceholder')"
                  class="input-field !py-1.5 pr-8 font-mono text-xs"
                />
                <button
                  type="button"
                  class="absolute right-2 top-1.5 text-slate-400 hover:text-slate-600"
                  :title="showCookie ? t('explore.auth.hideSecret') : t('explore.auth.showSecret')"
                  @click="showCookie = !showCookie"
                >
                  <EyeOff v-if="showCookie" class="h-3.5 w-3.5" />
                  <Eye v-else class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>

            <!-- Mode: Basic Auth -->
            <div v-else-if="authMode === 'basic'" class="grid grid-cols-1 gap-3 pt-1 sm:grid-cols-2">
              <div>
                <label class="mb-1 block text-[11px] font-medium text-slate-700">
                  {{ t("explore.auth.usernameLabel") }}
                </label>
                <input
                  v-model="basicUsername"
                  type="text"
                  :placeholder="t('explore.auth.usernamePlaceholder')"
                  class="input-field !py-1.5 font-mono text-xs"
                />
              </div>
              <div>
                <label class="mb-1 block text-[11px] font-medium text-slate-700">
                  {{ t("explore.auth.passwordLabel") }}
                </label>
                <div class="relative">
                  <input
                    v-model="basicPassword"
                    :type="showBasicPassword ? 'text' : 'password'"
                    :placeholder="t('explore.auth.passwordPlaceholder')"
                    class="input-field !py-1.5 pr-8 font-mono text-xs"
                  />
                  <button
                    type="button"
                    class="absolute right-2 top-1.5 text-slate-400 hover:text-slate-600"
                    :title="showBasicPassword ? t('explore.auth.hideSecret') : t('explore.auth.showSecret')"
                    @click="showBasicPassword = !showBasicPassword"
                  >
                    <EyeOff v-if="showBasicPassword" class="h-3.5 w-3.5" />
                    <Eye v-else class="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </div>

            <!-- Mode: Login API -->
            <div v-else-if="authMode === 'login_api'" class="space-y-3 pt-1">
              <div class="grid grid-cols-1 gap-3 sm:grid-cols-12">
                <div class="sm:col-span-6">
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.loginUrlLabel") }}
                  </label>
                  <input
                    v-model="loginApiUrl"
                    data-testid="explore-auth-login-url-input"
                    type="url"
                    :placeholder="t('explore.auth.loginUrlPlaceholder')"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div class="sm:col-span-2">
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.loginMethodLabel") }}
                  </label>
                  <select
                    v-model="loginApiMethod"
                    data-testid="explore-auth-login-method-select"
                    class="input-field !py-1.5 font-mono text-xs"
                  >
                    <option value="POST">POST</option>
                    <option value="PUT">PUT</option>
                    <option value="PATCH">PATCH</option>
                  </select>
                </div>
                <div class="sm:col-span-4">
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.contentTypeLabel") }}
                  </label>
                  <div class="flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-xs">
                    <button
                      type="button"
                      class="flex-1 rounded-md px-2 py-1 font-medium transition"
                      :class="
                        loginApiContentType === 'json'
                          ? 'bg-white text-indigo-700 shadow-sm'
                          : 'text-slate-600 hover:text-slate-900'
                      "
                      @click="loginApiContentType = 'json'"
                    >
                      JSON
                    </button>
                    <button
                      type="button"
                      class="flex-1 rounded-md px-2 py-1 font-medium transition"
                      :class="
                        loginApiContentType === 'form'
                          ? 'bg-white text-indigo-700 shadow-sm'
                          : 'text-slate-600 hover:text-slate-900'
                      "
                      @click="loginApiContentType = 'form'"
                    >
                      Form
                    </button>
                  </div>
                </div>
              </div>

              <div class="grid grid-cols-1 gap-3 sm:grid-cols-4">
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.usernameFieldLabel") }}
                  </label>
                  <input
                    v-model="loginApiUsernameField"
                    type="text"
                    placeholder="username"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.usernameLabel") }}
                  </label>
                  <input
                    v-model="loginApiUsername"
                    type="text"
                    :placeholder="t('explore.auth.usernamePlaceholder')"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.passwordFieldLabel") }}
                  </label>
                  <input
                    v-model="loginApiPasswordField"
                    type="text"
                    placeholder="password"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.passwordLabel") }}
                  </label>
                  <div class="relative">
                    <input
                      v-model="loginApiPassword"
                      :type="showLoginApiPassword ? 'text' : 'password'"
                      :placeholder="t('explore.auth.passwordPlaceholder')"
                      class="input-field !py-1.5 pr-8 font-mono text-xs"
                    />
                    <button
                      type="button"
                      class="absolute right-2 top-1.5 text-slate-400 hover:text-slate-600"
                      :title="showLoginApiPassword ? t('explore.auth.hideSecret') : t('explore.auth.showSecret')"
                      @click="showLoginApiPassword = !showLoginApiPassword"
                    >
                      <EyeOff v-if="showLoginApiPassword" class="h-3.5 w-3.5" />
                      <Eye v-else class="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              </div>

              <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.tokenJsonpathLabel") }}
                  </label>
                  <input
                    v-model="loginApiTokenJsonpath"
                    type="text"
                    :placeholder="t('explore.auth.tokenJsonpathPlaceholder')"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.targetHeaderLabel") }}
                  </label>
                  <input
                    v-model="loginApiTokenHeader"
                    type="text"
                    placeholder="Authorization"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.tokenPrefixLabel") }}
                  </label>
                  <input
                    v-model="loginApiTokenPrefix"
                    type="text"
                    placeholder="Bearer "
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
              </div>
            </div>

            <!-- Mode: Browser Form Login -->
            <div v-else-if="authMode === 'form_login'" class="space-y-3 pt-1">
              <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.formLoginUrlLabel") }}
                  </label>
                  <input
                    v-model="formLoginUrl"
                    type="url"
                    :placeholder="t('explore.auth.formLoginUrlPlaceholder')"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.usernameLabel") }}
                  </label>
                  <input
                    v-model="formLoginUsername"
                    type="text"
                    :placeholder="t('explore.auth.usernamePlaceholder')"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.passwordLabel") }}
                  </label>
                  <div class="relative">
                    <input
                      v-model="formLoginPassword"
                      :type="showFormLoginPassword ? 'text' : 'password'"
                      :placeholder="t('explore.auth.passwordPlaceholder')"
                      class="input-field !py-1.5 pr-8 font-mono text-xs"
                    />
                    <button
                      type="button"
                      class="absolute right-2 top-1.5 text-slate-400 hover:text-slate-600"
                      :title="showFormLoginPassword ? t('explore.auth.hideSecret') : t('explore.auth.showSecret')"
                      @click="showFormLoginPassword = !showFormLoginPassword"
                    >
                      <EyeOff v-if="showFormLoginPassword" class="h-3.5 w-3.5" />
                      <Eye v-else class="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              </div>

              <div>
                <button
                  type="button"
                  class="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500 hover:text-indigo-700"
                  @click="showFormSelectors = !showFormSelectors"
                >
                  <ChevronDown v-if="showFormSelectors" class="h-3 w-3" />
                  <ChevronRight v-else class="h-3 w-3" />
                  <span>{{ t("explore.auth.advancedSelectors") }}</span>
                </button>
                <div v-if="showFormSelectors" class="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <div>
                    <label class="mb-1 block text-[11px] font-medium text-slate-600">
                      {{ t("explore.auth.usernameSelectorLabel") }}
                    </label>
                    <input
                      v-model="formUsernameSelector"
                      type="text"
                      placeholder="input[name=username]"
                      class="input-field !py-1.5 font-mono text-xs"
                    />
                  </div>
                  <div>
                    <label class="mb-1 block text-[11px] font-medium text-slate-600">
                      {{ t("explore.auth.passwordSelectorLabel") }}
                    </label>
                    <input
                      v-model="formPasswordSelector"
                      type="text"
                      placeholder="input[type=password]"
                      class="input-field !py-1.5 font-mono text-xs"
                    />
                  </div>
                  <div>
                    <label class="mb-1 block text-[11px] font-medium text-slate-600">
                      {{ t("explore.auth.submitSelectorLabel") }}
                    </label>
                    <input
                      v-model="formSubmitSelector"
                      type="text"
                      placeholder="button[type=submit]"
                      class="input-field !py-1.5 font-mono text-xs"
                    />
                  </div>
                </div>
              </div>
            </div>

            <!-- Mode: Custom Headers -->
            <div v-else-if="authMode === 'headers'" class="space-y-2 pt-1">
              <div class="flex items-center justify-between">
                <span class="text-[11px] font-medium text-slate-700">
                  {{ t("explore.auth.customHeadersLabel") }}
                </span>
                <div class="flex items-center gap-3">
                  <button
                    type="button"
                    class="inline-flex items-center gap-1 text-[11px] text-slate-500 hover:text-slate-700"
                    @click="showCustomHeaderValues = !showCustomHeaderValues"
                  >
                    <EyeOff v-if="showCustomHeaderValues" class="h-3 w-3" />
                    <Eye v-else class="h-3 w-3" />
                    <span>
                      {{ showCustomHeaderValues ? t("explore.auth.hideSecret") : t("explore.auth.showSecret") }}
                    </span>
                  </button>
                  <button
                    type="button"
                    class="text-xs font-medium text-indigo-600 hover:text-indigo-800"
                    @click="customHeaderRows.push({ key: '', value: '' })"
                  >
                    {{ t("explore.auth.addHeaderRow") }}
                  </button>
                </div>
              </div>
              <div
                v-for="(row, idx) in customHeaderRows"
                :key="idx"
                class="flex items-center gap-2"
              >
                <input
                  v-model="row.key"
                  type="text"
                  :placeholder="t('explore.auth.headerNamePlaceholder')"
                  class="input-field !py-1.5 font-mono text-xs"
                />
                <span class="text-xs text-slate-400">:</span>
                <input
                  v-model="row.value"
                  :type="showCustomHeaderValues ? 'text' : 'password'"
                  :placeholder="t('explore.auth.headerValuePlaceholder')"
                  class="input-field !py-1.5 font-mono text-xs"
                />
                <button
                  type="button"
                  class="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-rose-600"
                  :disabled="customHeaderRows.length === 1"
                  @click="customHeaderRows.splice(idx, 1)"
                >
                  <X class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>

            <!-- Burp-Style Session Validity Check (Verify Auth) + Save as Project Profile -->
            <div
              v-if="authMode !== 'none'"
              class="space-y-3 border-t border-slate-100 pt-3"
            >
              <!-- Pre-flight Verify URL Bar -->
              <div class="flex flex-wrap items-end gap-2">
                <div class="min-w-[240px] flex-1">
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("explore.auth.verifyUrlLabel") }}
                  </label>
                  <input
                    v-model="verifyUrl"
                    data-testid="explore-verify-url-input"
                    type="url"
                    :placeholder="t('explore.auth.verifyUrlPlaceholder')"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <button
                  type="button"
                  data-testid="explore-verify-auth-btn"
                  class="btn-secondary !px-3 !py-1.5 !text-xs"
                  :disabled="verifyingAuth"
                  @click="handleVerifyAuth"
                >
                  <Loader2 v-if="verifyingAuth" class="h-3.5 w-3.5 animate-spin text-indigo-600" />
                  <ShieldCheck v-else class="h-3.5 w-3.5 text-indigo-600" />
                  <span>
                    {{ verifyingAuth ? t("explore.auth.verifyingBtn") : t("explore.auth.verifyBtn") }}
                  </span>
                </button>
              </div>

              <!-- Verify Auth Inline Result Badge -->
              <div
                v-if="verifyResult"
                data-testid="explore-verify-auth-result"
                class="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-xs"
                :class="
                  verifyResult.verdict === 'valid'
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                    : verifyResult.verdict === 'denied'
                      ? 'border-amber-200 bg-amber-50 text-amber-900'
                      : 'border-rose-200 bg-rose-50 text-rose-800'
                "
              >
                <CheckCircle2
                  v-if="verifyResult.verdict === 'valid'"
                  class="h-4 w-4 shrink-0 text-emerald-600"
                />
                <AlertTriangle
                  v-else
                  class="h-4 w-4 shrink-0"
                  :class="verifyResult.verdict === 'denied' ? 'text-amber-600' : 'text-rose-600'"
                />
                <span class="font-semibold">
                  {{
                    verifyResult.verdict === "valid"
                      ? t("explore.auth.verifyValid")
                      : verifyResult.verdict === "denied"
                        ? t("explore.auth.verifyDenied")
                        : t("explore.auth.verifyError")
                  }}
                </span>
                <span
                  v-if="verifyResult.status_code"
                  class="rounded bg-white/80 px-1.5 py-0.5 font-mono text-[11px] font-semibold ring-1 ring-inset ring-black/10"
                >
                  HTTP {{ verifyResult.status_code }}
                </span>
                <span
                  v-if="verifyResult.biz_code !== null && verifyResult.biz_code !== undefined"
                  class="rounded bg-white/80 px-1.5 py-0.5 font-mono text-[11px] ring-1 ring-inset ring-black/10"
                >
                  biz={{ verifyResult.biz_code }}
                </span>
                <span class="font-mono text-[11px] opacity-80">
                  {{ verifyResult.duration_ms }}ms
                </span>
                <span v-if="verifyResult.message" class="text-[11px]">
                  · {{ verifyResult.message }}
                </span>
                <span
                  v-if="verifyResult.redirected_to"
                  class="font-mono text-[11px] underline decoration-dotted"
                >
                  ({{ t("explore.auth.verifyRedirect", { url: verifyResult.redirected_to }) }})
                </span>
              </div>

              <div
                v-if="verifyError"
                class="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700"
              >
                {{ verifyError }}
              </div>

              <!-- Optional Save as Project AuthProfile Toggle -->
              <div
                v-if="projectId && authMode !== 'profile'"
                class="flex flex-wrap items-center gap-3 rounded-lg bg-slate-50 px-3 py-2 text-xs"
              >
                <label class="inline-flex cursor-pointer items-center gap-2 font-medium text-slate-700">
                  <input
                    v-model="saveAsProfile"
                    data-testid="explore-auth-save-toggle"
                    type="checkbox"
                    class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                  />
                  <span>{{ t("explore.auth.saveAsProfileToggle") }}</span>
                </label>

                <template v-if="saveAsProfile">
                  <input
                    v-model="saveProfileName"
                    data-testid="explore-auth-profile-name-input"
                    type="text"
                    :placeholder="t('explore.auth.profileNamePlaceholder')"
                    class="input-field w-48 !py-1 text-xs"
                  />
                  <input
                    v-model="saveProfileRoleTag"
                    data-testid="explore-auth-role-tag-input"
                    list="explore-role-tag-list"
                    type="text"
                    :placeholder="t('explore.auth.roleTagPlaceholder')"
                    class="input-field w-36 !py-1 font-mono text-xs"
                  />
                  <datalist id="explore-role-tag-list">
                    <option v-for="rTag in ROLE_TAG_SUGGESTIONS" :key="rTag" :value="rTag" />
                  </datalist>
                </template>
              </div>
            </div>
          </div>
        </div>

        <!-- Auth Profile Saved Notice Toast -->
        <div
          v-if="authSuccessNotice"
          data-testid="explore-auth-saved-toast"
          class="flex items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-xs font-medium text-emerald-800"
        >
          <span class="flex items-center gap-2">
            <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-600" />
            <span>{{ authSuccessNotice }}</span>
          </span>
          <button
            type="button"
            class="text-emerald-600 hover:text-emerald-800"
            @click="authSuccessNotice = null"
          >
            <X class="h-3.5 w-3.5" />
          </button>
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
            :disabled="running || runningPortals"
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

    <!-- Real-Time Async Explore Progress Card & 5-Stage Stepper -->
    <div
      v-if="activeJob"
      data-testid="explore-progress-card"
      class="overflow-hidden rounded-xl border shadow-sm transition"
      :class="
        activeJob.status === 'failed'
          ? 'border-rose-200 bg-rose-50/40'
          : activeJob.status === 'completed'
            ? 'border-emerald-200 bg-emerald-50/40'
            : 'border-indigo-200 bg-white'
      "
    >
      <div class="space-y-4 px-5 py-4">
        <!-- Header Row: Status, Percentage, Elapsed, Endpoints Badge -->
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-2.5">
            <Loader2
              v-if="activeJob.status === 'running'"
              class="h-4 w-4 shrink-0 animate-spin text-indigo-600"
            />
            <CheckCircle2
              v-else-if="activeJob.status === 'completed'"
              class="h-4 w-4 shrink-0 text-emerald-600"
            />
            <AlertTriangle v-else class="h-4 w-4 shrink-0 text-rose-600" />
            <span class="text-sm font-semibold text-slate-900">
              {{
                activeJob.status === "completed"
                  ? t("explore.progress.titleCompleted")
                  : activeJob.status === "failed"
                    ? t("explore.progress.titleFailed")
                    : t("explore.progress.titleRunning")
              }}
            </span>
            <span
              v-if="activeJob.endpoints_found > 0"
              class="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 font-mono text-[11px] font-semibold text-emerald-700"
            >
              {{ t("explore.progress.endpointsBadge", { count: activeJob.endpoints_found }) }}
            </span>
          </div>

          <div class="flex items-center gap-3 text-xs">
            <span class="inline-flex items-center gap-1 font-mono text-slate-500">
              <Clock class="h-3.5 w-3.5" />
              <span>{{ t("explore.progress.elapsed", { sec: elapsedSeconds }) }}</span>
            </span>
            <span
              class="font-mono text-base font-bold"
              :class="
                activeJob.status === 'failed'
                  ? 'text-rose-700'
                  : activeJob.status === 'completed'
                    ? 'text-emerald-700'
                    : 'text-indigo-700'
              "
            >
              {{ activeJobPct }}%
            </span>
            <button
              v-if="activeJob.status !== 'running'"
              type="button"
              class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-slate-500 hover:bg-slate-200/60 hover:text-slate-800"
              :title="t('explore.progress.dismiss')"
              @click="dismissProgressCard"
            >
              <X class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <!-- Smooth Progress Bar -->
        <div class="h-2 w-full overflow-hidden rounded-full bg-slate-100">
          <div
            data-testid="explore-progress-bar"
            class="h-full rounded-full transition-all duration-500 ease-out"
            :class="
              activeJob.status === 'failed'
                ? 'bg-rose-500'
                : activeJob.status === 'completed'
                  ? 'bg-emerald-500'
                  : 'bg-indigo-600'
            "
            :style="{ width: `${activeJobPct}%` }"
          />
        </div>

        <!-- 5-Step Horizontal Stage Stepper -->
        <div class="grid grid-cols-2 gap-2 sm:grid-cols-5">
          <div
            v-for="(stageKey, idx) in PROGRESS_STAGES"
            :key="stageKey"
            class="flex items-center gap-2 rounded-lg border px-2.5 py-2 text-xs transition"
            :class="
              idx < activeStageIndex
                ? 'border-emerald-200 bg-emerald-50/70 text-emerald-800'
                : idx === activeStageIndex && activeJob.status === 'running'
                  ? 'border-indigo-300 bg-indigo-50 text-indigo-800 shadow-2xs'
                  : idx === activeStageIndex && activeJob.status === 'failed'
                    ? 'border-rose-300 bg-rose-50 text-rose-800'
                    : 'border-slate-200 bg-slate-50/70 text-slate-400'
            "
          >
            <CheckCircle2
              v-if="idx < activeStageIndex"
              class="h-3.5 w-3.5 shrink-0 text-emerald-600"
            />
            <Loader2
              v-else-if="idx === activeStageIndex && activeJob.status === 'running'"
              class="h-3.5 w-3.5 shrink-0 animate-spin text-indigo-600"
            />
            <AlertTriangle
              v-else-if="idx === activeStageIndex && activeJob.status === 'failed'"
              class="h-3.5 w-3.5 shrink-0 text-rose-600"
            />
            <span
              v-else
              class="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border border-slate-300 font-mono text-[9px] font-semibold text-slate-400"
            >
              {{ idx + 1 }}
            </span>
            <span class="truncate font-medium">
              {{ t(`explore.progress.stage.${stageKey}`) }}
            </span>
          </div>
        </div>

        <!-- Live Target & Portal Counter Strip -->
        <div
          v-if="activeJob.current_target || activeJob.targets_total > 0"
          class="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-600"
        >
          <div v-if="activeJob.current_target" class="flex min-w-0 items-center gap-1.5">
            <span class="shrink-0 font-medium text-slate-500">
              {{ t("explore.progress.scanning") }}
            </span>
            <span class="truncate font-mono text-slate-800">
              {{ activeJob.current_target }}
            </span>
          </div>
          <span
            v-if="activeJob.targets_total > 0"
            class="shrink-0 rounded-full bg-slate-100 px-2.5 py-0.5 font-mono text-[11px] font-semibold text-slate-700"
          >
            {{
              t("explore.progress.portalsCounter", {
                done: Math.min(activeJob.targets_done, activeJob.targets_total),
                total: activeJob.targets_total,
              })
            }}
          </span>
        </div>
      </div>
    </div>

    <!-- Empty State before any Explore -->
    <EmptyState
      v-if="!result && !running && !runningPortals && !activeJob"
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
      <div class="space-y-2.5 rounded-xl border border-slate-200 bg-white px-5 py-3 text-xs shadow-sm">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex flex-wrap items-center gap-3">
            <span class="rounded-md bg-indigo-50 px-2 py-0.5 font-mono font-semibold text-indigo-700">
              {{ result.seed.root_domain || result.explore_id.slice(0, 12) }}
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
              v-if="resultAuthBadgeText"
              data-testid="explore-result-auth-badge"
              class="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2.5 py-0.5 font-mono text-[11px] font-medium text-emerald-700 ring-1 ring-emerald-600/20"
            >
              <KeyRound class="h-3 w-3 shrink-0" />
              <span>{{ t("explore.auth.resultBadge", { detail: resultAuthBadgeText }) }}</span>
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

        <!-- Merged Portal Hosts Strip (multi-domain visibility) -->
        <div
          v-if="resultMergedHosts.length > 0"
          data-testid="explore-merged-hosts"
          class="flex flex-wrap items-center gap-1.5 border-t border-slate-100 pt-2.5"
        >
          <span class="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500">
            <Globe class="h-3 w-3 text-indigo-500" />
            <span>{{ t("explore.mergedHostsLabel", { count: resultMergedHosts.length }) }}:</span>
          </span>
          <span
            v-for="host in resultMergedHosts"
            :key="host"
            class="rounded-md bg-indigo-50/70 px-2 py-0.5 font-mono text-[11px] font-medium text-indigo-800 ring-1 ring-inset ring-indigo-200/80"
          >
            {{ host }}
          </span>
        </div>

        <!-- Accumulated Scans Strip (when multiple scans merged into this domain inventory) -->
        <div
          v-if="result.scans && result.scans.length > 1"
          data-testid="explore-scans-strip"
          class="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-2.5"
        >
          <span class="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500">
            <GitMerge class="h-3 w-3 text-indigo-500" />
            <span>{{ t("explore.scansHistoryTitle", { count: result.scans.length }) }}:</span>
          </span>
          <span
            v-for="scan in result.scans"
            :key="scan.scan_id"
            class="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11px] text-slate-700"
          >
            <span class="font-medium text-slate-900">
              {{ scan.seed_type === "curl" ? t("explore.scanTypeCurl") : t("explore.scanTypeUrl") }}
            </span>
            <span v-if="scan.new_endpoints > 0" class="font-mono text-indigo-700">
              {{ t("explore.scanDeltaEndpoints", { n: scan.new_endpoints }) }}
            </span>
            <span v-if="scan.new_subdomains > 0" class="font-mono text-emerald-700">
              {{ t("explore.scanDeltaSubdomains", { n: scan.new_subdomains }) }}
            </span>
            <span
              v-if="scan.new_endpoints === 0 && scan.new_subdomains === 0"
              class="text-slate-400"
            >
              {{ t("explore.scanNoDelta") }}
            </span>
            <span v-if="scan.scanned_at" class="font-mono text-[10px] text-slate-400">
              · {{ formatShortTime(scan.scanned_at) }}
            </span>
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

    <!-- Agent HAR Exploration Prompt Modal -->
    <AcceptancePromptModal
      :open="harPromptOpen"
      :project-id="projectId"
      initial-mode="har_explore"
      @close="harPromptOpen = false"
    />
  </div>
</template>
