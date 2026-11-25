<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Copy,
  ExternalLink,
  KeyRound,
  ListChecks,
  Loader2,
  Plus,
  Send,
  ShieldAlert,
  Sparkles,
  Trash2,
  Users,
  X,
  XCircle,
} from "lucide-vue-next";
import {
  createAuthProfile,
  extractCredentialFromHar,
  listAuthProfiles,
  listTargets,
  replayEndpoint,
  replayEndpointMatrix,
} from "@/api/projects";
import type {
  AssertionEvalResult,
  AuthProfile,
  ReplayMatrixResponse,
  ReplayResponse,
  RoleTag,
  ScenarioAssertion,
} from "@/api/types";
import { useI18n } from "@/i18n";
import { copyToClipboard } from "@/utils/clipboard";
import { hasSyncFeedback } from "@/utils/gateAndExport";

export interface TryItEndpointInput {
  method: string;
  path: string;
  host?: string;
  baseUrl?: string;
  summary?: string;
  parameters?: Array<Record<string, unknown>>;
  requestExample?: unknown;
  customHeaders?: Record<string, string>;
}

interface KvRow {
  key: string;
  value: string;
}

const props = defineProps<{
  open: boolean;
  projectId: string;
  endpoint: TryItEndpointInput | null;
  assertions?: ScenarioAssertion[];
}>();

const emit = defineEmits<{
  close: [];
  synced: [payload: { synced_scenarios: number; synced_assertions: number }];
}>();

const { t } = useI18n();

const HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] as const;
const ROLE_PRESETS = ["admin", "pharmacy", "doctor", "assistant", "account_a", "account_b", "guest"] as const;

const SKIP_AUTH_HEADERS = new Set([
  "authorization",
  "cookie",
  "x-auth-token",
  "x-api-key",
  "x-access-token",
  "token",
  "access-token",
  "set-cookie",
]);

const SKIP_EPHEMERAL_HEADERS = new Set([
  "b3",
  "traceparent",
  "tracestate",
  "x-b3-traceid",
  "x-b3-spanid",
  "x-b3-parentspanid",
  "x-b3-sampled",
  "sentry-trace",
  "baggage",
  "x-request-id",
  "x-correlation-id",
  "host",
  "content-length",
  "origin",
  "referer",
  "user-agent",
  "connection",
]);

function isRedactedHeaderValue(value: string): boolean {
  const lower = value.toLowerCase();
  return lower.includes("***") || lower.includes("<redacted>") || lower.includes("(present)");
}

function shouldSkipPrefillHeader(key: string, value: string, allowEmptyValue = false): boolean {
  const cleanKey = key.trim();
  const cleanVal = value.trim();
  if (!cleanKey) return true;
  if (!allowEmptyValue && !cleanVal) return true;
  if (cleanVal && isRedactedHeaderValue(cleanVal)) return true;
  const lowerKey = cleanKey.toLowerCase();
  return SKIP_AUTH_HEADERS.has(lowerKey) || SKIP_EPHEMERAL_HEADERS.has(lowerKey);
}

const method = ref("GET");
const targetUrl = ref("");
const credentialId = ref("");
const timeoutSec = ref(15);
const queryRows = ref<KvRow[]>([]);
const headerRows = ref<KvRow[]>([{ key: "Accept", value: "application/json" }]);
const bodyText = ref("");

const authProfiles = ref<AuthProfile[]>([]);
const userClearedCredential = ref(false);
const defaultBaseUrl = ref("");
const sending = ref(false);
const runningMatrix = ref(false);
const errorMsg = ref<string | null>(null);
const response = ref<ReplayResponse | null>(null);
const matrixResponse = ref<ReplayMatrixResponse | null>(null);
const expandedMatrixRows = ref<Set<number>>(new Set());
const respTab = ref<"body" | "headers">("body");
const copiedResp = ref(false);
const runAssertions = ref(true);
const syncScenarios = ref(true);

const activeAssertions = computed<ScenarioAssertion[]>(() => props.assertions ?? []);
const assertionResults = computed<AssertionEvalResult[]>(
  () => response.value?.assertion_results ?? []
);
const assertionPassCount = computed(
  () => assertionResults.value.filter((a) => a.passed).length
);

function formatAssertionVal(val: unknown): string {
  if (val === undefined) return "undefined";
  if (val === null) return "null";
  if (typeof val === "string") {
    return val.length > 60 ? `${val.slice(0, 57)}...` : val;
  }
  try {
    const serialized = JSON.stringify(val);
    return serialized.length > 60 ? `${serialized.slice(0, 57)}...` : serialized;
  } catch {
    return String(val);
  }
}

// Inline Quick Add Credential mini-form state
const showQuickAddCred = ref(false);
const quickCredSaving = ref(false);
const quickCredError = ref<string | null>(null);
const extractingHarCred = ref(false);
const credNotice = ref<string | null>(null);
const quickCredName = ref("");
const quickCredRole = ref<RoleTag>("account_a");
const quickCredPreset = ref<"bearer" | "cookie" | "custom">("bearer");
const quickCredHeaderName = ref("Authorization");
const quickCredHeaderValue = ref("");

function applyQuickPreset(preset: "bearer" | "cookie" | "custom"): void {
  quickCredPreset.value = preset;
  if (preset === "bearer") {
    quickCredHeaderName.value = "Authorization";
  } else if (preset === "cookie") {
    quickCredHeaderName.value = "Cookie";
  } else {
    if (
      quickCredHeaderName.value === "Authorization" ||
      quickCredHeaderName.value === "Cookie"
    ) {
      quickCredHeaderName.value = "X-Auth-Token";
    }
  }
}

function toggleQuickAddCred(): void {
  showQuickAddCred.value = !showQuickAddCred.value;
  quickCredError.value = null;
  if (showQuickAddCred.value && !quickCredName.value) {
    quickCredName.value =
      authProfiles.value.length === 0
        ? "Account A"
        : `Account ${String.fromCharCode(65 + Math.min(authProfiles.value.length, 25))}`;
  }
}

function onCredentialSelectChange(event: Event): void {
  const val = (event.target as HTMLSelectElement).value;
  credentialId.value = val;
  userClearedCredential.value = val === "";
}

async function handleQuickAddCredential(): Promise<void> {
  if (!props.projectId || !quickCredName.value.trim() || !quickCredHeaderName.value.trim() || !quickCredHeaderValue.value.trim()) {
    return;
  }
  quickCredSaving.value = true;
  quickCredError.value = null;
  try {
    let resolvedValue = quickCredHeaderValue.value.trim();
    if (
      quickCredPreset.value === "bearer" &&
      !/^bearer\s+/i.test(resolvedValue) &&
      !resolvedValue.startsWith("ENV:")
    ) {
      resolvedValue = `Bearer ${resolvedValue}`;
    }
    const created = await createAuthProfile(props.projectId, {
      name: quickCredName.value.trim(),
      role_tag: quickCredRole.value,
      extra_headers_ref: {
        [quickCredHeaderName.value.trim()]: resolvedValue,
      },
    });
    authProfiles.value = [...authProfiles.value, created];
    credentialId.value = created.id;
    userClearedCredential.value = false;
    showQuickAddCred.value = false;
    quickCredName.value = "";
    quickCredHeaderValue.value = "";
  } catch (err) {
    quickCredError.value = err instanceof Error ? err.message : t("tryit.quickErrCreate");
  } finally {
    quickCredSaving.value = false;
  }
}

async function handleExtractHarCredential(): Promise<void> {
  if (!props.projectId || extractingHarCred.value) return;
  extractingHarCred.value = true;
  errorMsg.value = null;
  credNotice.value = null;
  try {
    const res = await extractCredentialFromHar(props.projectId);
    const exists = authProfiles.value.some((p) => p.id === res.profile.id);
    authProfiles.value = exists
      ? authProfiles.value.map((p) => (p.id === res.profile.id ? res.profile : p))
      : [...authProfiles.value, res.profile];
    credentialId.value = res.profile.id;
    userClearedCredential.value = false;
    const headersText = res.extracted_header_names.join(", ") || "auth";
    credNotice.value = t("tryit.extractHarSuccess", { headers: headersText });
    window.setTimeout(() => {
      credNotice.value = null;
    }, 5000);
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("tryit.extractHarErr");
  } finally {
    extractingHarCred.value = false;
  }
}

const bodySupported = computed(() => !["GET", "HEAD"].includes(method.value.toUpperCase()));

const responseHeaderEntries = computed(() =>
  Object.entries(response.value?.response_headers ?? {})
);

const formattedResponseBody = computed(() => {
  if (!response.value) return "";
  if (response.value.body_json !== undefined && response.value.body_json !== null) {
    try {
      return JSON.stringify(response.value.body_json, null, 2);
    } catch {
      return response.value.body_text || "";
    }
  }
  const raw = response.value.body_text || "";
  if (raw.trim().startsWith("{") || raw.trim().startsWith("[")) {
    try {
      return JSON.stringify(JSON.parse(raw), null, 2);
    } catch {
      return raw;
    }
  }
  return raw;
});

const isHtmlResponse = computed(() => {
  if (!response.value) return false;
  const ct = (response.value.content_type || "").toLowerCase();
  if (ct.includes("text/html")) return true;
  const bodyStart = (response.value.body_text || "").trimStart().toLowerCase();
  return bodyStart.startsWith("<!doctype html") || bodyStart.startsWith("<html");
});

function statusColorClass(code: number): string {
  if (code >= 200 && code < 300) return "bg-emerald-50 text-emerald-700 ring-emerald-600/20";
  if (code >= 300 && code < 400) return "bg-sky-50 text-sky-700 ring-sky-600/20";
  if (code >= 400 && code < 500) return "bg-amber-50 text-amber-800 ring-amber-600/20";
  return "bg-rose-50 text-rose-700 ring-rose-600/20";
}

function buildPrefilledUrl(ep: TryItEndpointInput | null, fallbackBase: string): string {
  if (!ep) return "";
  const rawPath = (ep.path || "/").trim();
  if (/^https?:\/\//i.test(rawPath)) {
    return rawPath;
  }
  const normalizedPath = rawPath.startsWith("/") ? rawPath : `/${rawPath}`;
  if (ep.baseUrl && /^https?:\/\//i.test(ep.baseUrl)) {
    return `${ep.baseUrl.replace(/\/+$/, "")}${normalizedPath}`;
  }
  const rawHost = (ep.host || "").trim();
  if (rawHost) {
    if (/^https?:\/\//i.test(rawHost)) {
      return `${rawHost.replace(/\/+$/, "")}${normalizedPath}`;
    }
    const cleanHost = rawHost.replace(/\/+$/, "");
    const scheme =
      cleanHost.startsWith("127.0.0.1") || cleanHost.startsWith("localhost") ? "http" : "https";
    return `${scheme}://${cleanHost}${normalizedPath}`;
  }
  if (fallbackBase) {
    return `${fallbackBase.replace(/\/+$/, "")}${normalizedPath}`;
  }
  return `https://api.example.com${normalizedPath}`;
}

async function loadProjectContext(pid: string): Promise<void> {
  if (!pid) {
    authProfiles.value = [];
    defaultBaseUrl.value = "";
    credentialId.value = "";
    return;
  }
  const [profilesRes, targetsRes] = await Promise.allSettled([
    listAuthProfiles(pid),
    listTargets(pid),
  ]);
  if (profilesRes.status === "fulfilled") {
    authProfiles.value = profilesRes.value;
    const stillValid = authProfiles.value.some((p) => p.id === credentialId.value);
    if (!stillValid) {
      credentialId.value = "";
    }
    if (
      authProfiles.value.length > 0 &&
      !credentialId.value &&
      !userClearedCredential.value
    ) {
      credentialId.value = authProfiles.value[0].id;
    }
  }
  if (targetsRes.status === "fulfilled" && targetsRes.value.length > 0) {
    defaultBaseUrl.value = targetsRes.value[0].base_url || "";
  }
}

watch(
  () => props.projectId,
  () => {
    credentialId.value = "";
    userClearedCredential.value = false;
    showQuickAddCred.value = false;
  }
);

async function hydrateFromEndpoint(): Promise<void> {
  if (!props.open) return;
  response.value = null;
  matrixResponse.value = null;
  expandedMatrixRows.value = new Set();
  errorMsg.value = null;
  respTab.value = "body";
  runAssertions.value = true;
  syncScenarios.value = true;

  await loadProjectContext(props.projectId);

  const ep = props.endpoint;
  if (!ep) return;

  method.value = (ep.method || "GET").toUpperCase();
  targetUrl.value = buildPrefilledUrl(ep, defaultBaseUrl.value);

  // Extract query parameters and headers from OpenAPI parameters list & captured customHeaders
  const qRows: KvRow[] = [];
  const hRows: KvRow[] = [];
  const seenHeaders = new Map<string, number>();

  for (const param of ep.parameters ?? []) {
    const pIn = String(param.in ?? "").toLowerCase();
    const pName = String(param.name ?? "").trim();
    const pVal = param.example !== undefined && param.example !== null ? String(param.example).trim() : "";
    if (!pName) continue;
    if (pIn === "query") {
      qRows.push({ key: pName, value: pVal });
    } else if (pIn === "header") {
      const lowerKey = pName.toLowerCase();
      if (shouldSkipPrefillHeader(pName, pVal, lowerKey !== "accept")) continue;
      if (!seenHeaders.has(lowerKey)) {
        seenHeaders.set(lowerKey, hRows.length);
        hRows.push({ key: pName, value: pVal });
      }
    }
  }

  for (const [rawKey, rawVal] of Object.entries(ep.customHeaders ?? {})) {
    const cleanKey = rawKey.trim();
    const cleanVal = rawVal !== undefined && rawVal !== null ? String(rawVal).trim() : "";
    if (shouldSkipPrefillHeader(cleanKey, cleanVal, false)) continue;
    const lowerKey = cleanKey.toLowerCase();
    const existingIdx = seenHeaders.get(lowerKey);
    if (existingIdx !== undefined) {
      if (!hRows[existingIdx].value) {
        hRows[existingIdx].value = cleanVal;
      }
    } else {
      seenHeaders.set(lowerKey, hRows.length);
      hRows.push({ key: cleanKey, value: cleanVal });
    }
  }

  if (!seenHeaders.has("accept")) {
    hRows.unshift({ key: "Accept", value: "application/json" });
  }

  queryRows.value = qRows;
  headerRows.value = hRows;

  if (ep.requestExample !== undefined && ep.requestExample !== null) {
    bodyText.value =
      typeof ep.requestExample === "string"
        ? ep.requestExample
        : JSON.stringify(ep.requestExample, null, 2);
  } else {
    bodyText.value = "";
  }
}

watch(
  () => [props.open, props.projectId, props.endpoint] as const,
  ([isOpen]) => {
    if (isOpen) {
      void hydrateFromEndpoint();
    }
  },
  { immediate: true }
);

function formatJsonBody(): void {
  errorMsg.value = null;
  const trimmed = bodyText.value.trim();
  if (!trimmed) return;
  try {
    bodyText.value = JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    errorMsg.value = t("tryit.errInvalidJson");
  }
}

async function handleSend(): Promise<void> {
  errorMsg.value = null;
  const url = targetUrl.value.trim();
  if (!url || !/^https?:\/\//i.test(url)) {
    errorMsg.value = t("tryit.errMissingUrl");
    return;
  }

  const headers: Record<string, string> = {};
  for (const row of headerRows.value) {
    if (row.key.trim()) {
      headers[row.key.trim()] = row.value;
    }
  }

  const queryParams: Record<string, string> = {};
  for (const row of queryRows.value) {
    if (row.key.trim()) {
      queryParams[row.key.trim()] = row.value;
    }
  }

  let parsedBody: unknown = undefined;
  if (bodySupported.value && bodyText.value.trim()) {
    const raw = bodyText.value.trim();
    try {
      parsedBody = JSON.parse(raw);
    } catch {
      parsedBody = raw;
    }
  }

  sending.value = true;
  response.value = null;
  try {
    const hasPropAssertions = activeAssertions.value.length > 0;
    const shouldAttachAssertions = runAssertions.value && hasPropAssertions;
    const firstDbDsnRef = shouldAttachAssertions
      ? activeAssertions.value.find((a) => a.db_dsn_ref?.trim())?.db_dsn_ref?.trim() || null
      : null;

    response.value = await replayEndpoint(props.projectId, {
      method: method.value,
      url,
      headers,
      query_params: queryParams,
      body: parsedBody,
      credential_id: credentialId.value || null,
      timeout_sec: timeoutSec.value,
      include_scenario_assertions: hasPropAssertions ? runAssertions.value : true,
      sync_scenarios: props.projectId
        ? hasPropAssertions
          ? runAssertions.value && syncScenarios.value
          : syncScenarios.value
        : false,
      ...(shouldAttachAssertions ? { assertions: activeAssertions.value } : {}),
      ...(firstDbDsnRef ? { db_dsn_ref: firstDbDsnRef } : {}),
    });
    respTab.value = "body";
    if (hasSyncFeedback(response.value)) {
      emit("synced", {
        synced_scenarios: response.value.synced_scenarios ?? 0,
        synced_assertions: response.value.synced_assertions ?? 0,
      });
    }
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("tryit.errSend");
  } finally {
    sending.value = false;
  }
}

function toggleMatrixRow(idx: number): void {
  const next = new Set(expandedMatrixRows.value);
  if (next.has(idx)) next.delete(idx);
  else next.add(idx);
  expandedMatrixRows.value = next;
}

function verdictBadgeClass(verdict: string): string {
  if (verdict === "allowed") return "bg-emerald-50 text-emerald-700 ring-emerald-600/25";
  if (verdict === "denied") return "bg-slate-100 text-slate-700 ring-slate-500/25";
  return "bg-rose-50 text-rose-700 ring-rose-600/25";
}

function formatByteSize(bytes: number): string {
  if (!bytes || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

async function handleRunRoleMatrix(): Promise<void> {
  errorMsg.value = null;
  const url = targetUrl.value.trim();
  if (!url || !/^https?:\/\//i.test(url)) {
    errorMsg.value = t("tryit.errMissingUrl");
    return;
  }

  const headers: Record<string, string> = {};
  for (const row of headerRows.value) {
    if (row.key.trim()) {
      headers[row.key.trim()] = row.value;
    }
  }

  const queryParams: Record<string, string> = {};
  for (const row of queryRows.value) {
    if (row.key.trim()) {
      queryParams[row.key.trim()] = row.value;
    }
  }

  let parsedBody: unknown = undefined;
  if (bodySupported.value && bodyText.value.trim()) {
    const raw = bodyText.value.trim();
    try {
      parsedBody = JSON.parse(raw);
    } catch {
      parsedBody = raw;
    }
  }

  runningMatrix.value = true;
  matrixResponse.value = null;
  expandedMatrixRows.value = new Set();
  try {
    matrixResponse.value = await replayEndpointMatrix(props.projectId, {
      method: method.value,
      url,
      headers,
      query_params: queryParams,
      body: parsedBody,
      credential_ids: null,
      include_anonymous: true,
      timeout_sec: timeoutSec.value,
    });
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("tryit.matrixErr");
  } finally {
    runningMatrix.value = false;
  }
}

async function copyResponseBody(): Promise<void> {
  if (!formattedResponseBody.value) return;
  const ok = await copyToClipboard(formattedResponseBody.value);
  if (!ok) return;
  copiedResp.value = true;
  window.setTimeout(() => {
    copiedResp.value = false;
  }, 1800);
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex justify-end bg-slate-950/50 backdrop-blur-sm"
      data-testid="try-it-drawer"
      @click.self="emit('close')"
    >
      <div class="flex h-full w-full max-w-2xl flex-col bg-white shadow-2xl">
        <!-- Header -->
        <div class="flex items-start justify-between gap-3 border-b border-slate-200 px-6 py-4">
          <div class="min-w-0">
            <h2 class="text-base font-bold text-slate-900">{{ t("tryit.title") }}</h2>
            <p class="mt-0.5 text-xs text-slate-500">{{ t("tryit.subtitle") }}</p>
          </div>
          <button
            type="button"
            class="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
            @click="emit('close')"
          >
            <X class="h-5 w-5" />
          </button>
        </div>

        <!-- Scrollable Body -->
        <div class="flex-1 space-y-5 overflow-y-auto px-6 py-5">
          <!-- Method + URL bar -->
          <div class="space-y-3">
            <div class="flex gap-2">
              <select
                v-model="method"
                data-testid="try-it-method"
                class="w-28 shrink-0 rounded-lg border border-slate-300 bg-slate-50 px-2.5 py-2 font-mono text-xs font-bold text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
              >
                <option v-for="m in HTTP_METHODS" :key="m" :value="m">{{ m }}</option>
              </select>
              <input
                v-model="targetUrl"
                data-testid="try-it-url"
                type="url"
                :placeholder="t('tryit.urlPlaceholder')"
                class="input-field font-mono text-xs"
              />
            </div>

            <!-- Credential + Timeout row -->
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div class="sm:col-span-2">
                <div class="mb-1 flex flex-wrap items-center justify-between gap-2">
                  <label class="flex items-center gap-1 text-xs font-medium text-slate-700">
                    <KeyRound class="h-3.5 w-3.5 text-indigo-500" />
                    <span>{{ t("tryit.credentialLabel") }}</span>
                  </label>
                  <div v-if="projectId" class="flex flex-wrap items-center gap-2 text-[11px]">
                    <button
                      type="button"
                      data-testid="extract-har-cred-btn"
                      :disabled="extractingHarCred"
                      class="inline-flex items-center gap-1 font-semibold text-emerald-700 transition hover:text-emerald-900 disabled:opacity-50"
                      @click="handleExtractHarCredential"
                    >
                      <Loader2 v-if="extractingHarCred" class="h-3 w-3 animate-spin" />
                      <Sparkles v-else class="h-3 w-3" />
                      <span>{{ extractingHarCred ? t("tryit.extractingHarBtn") : t("tryit.extractHarBtn") }}</span>
                    </button>
                    <span class="text-slate-300">|</span>
                    <button
                      type="button"
                      data-testid="try-it-quick-add-cred-btn"
                      class="font-semibold text-indigo-600 transition hover:text-indigo-800"
                      @click="toggleQuickAddCred"
                    >
                      {{ t("tryit.quickAddBtn") }}
                    </button>
                    <span class="text-slate-300">|</span>
                    <router-link
                      :to="`/p/${encodeURIComponent(projectId)}/settings?tab=auth`"
                      data-testid="try-it-manage-creds-link"
                      class="inline-flex items-center gap-0.5 font-medium text-slate-500 transition hover:text-indigo-600"
                      @click="emit('close')"
                    >
                      <span>{{ t("tryit.manageCreds") }}</span>
                      <ExternalLink class="h-2.5 w-2.5" />
                    </router-link>
                  </div>
                </div>
                <select
                  :value="credentialId"
                  data-testid="try-it-credential"
                  class="input-field py-1.5 text-xs"
                  @change="onCredentialSelectChange"
                >
                  <option value="">{{ t("tryit.credentialNone") }}</option>
                  <option v-for="cred in authProfiles" :key="cred.id" :value="cred.id">
                    {{ cred.name }} ({{ cred.role_tag }})
                  </option>
                </select>
                <p
                  v-if="credNotice"
                  data-testid="try-it-cred-notice"
                  class="mt-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-[11px] text-emerald-800"
                >
                  {{ credNotice }}
                </p>
                <p
                  v-if="authProfiles.length === 0 && !showQuickAddCred"
                  data-testid="try-it-no-creds-hint"
                  class="mt-1.5 rounded-lg border border-amber-200 bg-amber-50/80 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-900"
                >
                  {{ t("tryit.noCredsHint") }}
                </p>
              </div>
              <div>
                <label class="mb-1 block text-xs font-medium text-slate-700">
                  {{ t("tryit.timeoutLabel") }}
                </label>
                <input
                  v-model.number="timeoutSec"
                  type="number"
                  min="1"
                  max="60"
                  class="input-field py-1.5 font-mono text-xs"
                />
              </div>
            </div>

            <!-- Inline Quick-Add Credential Mini-Form -->
            <div
              v-if="showQuickAddCred && projectId"
              data-testid="try-it-quick-add-form"
              class="space-y-3 rounded-xl border border-indigo-200 bg-indigo-50/50 p-3.5"
            >
              <div class="flex items-center justify-between">
                <span class="flex items-center gap-1.5 text-xs font-semibold text-indigo-950">
                  <KeyRound class="h-3.5 w-3.5 text-indigo-600" />
                  <span>{{ t("tryit.quickAddTitle") }}</span>
                </span>
                <button
                  type="button"
                  class="rounded p-0.5 text-slate-400 hover:bg-white hover:text-slate-600"
                  @click="showQuickAddCred = false"
                >
                  <X class="h-3.5 w-3.5" />
                </button>
              </div>

              <div
                v-if="quickCredError"
                class="rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[11px] text-rose-700"
              >
                {{ quickCredError }}
              </div>

              <div class="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("tryit.quickNameLabel") }}
                  </label>
                  <input
                    v-model="quickCredName"
                    data-testid="quick-cred-name-input"
                    type="text"
                    :placeholder="t('tryit.quickNamePlaceholder')"
                    class="input-field !py-1.5 text-xs"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("tryit.quickRoleLabel") }}
                  </label>
                  <input
                    v-model="quickCredRole"
                    data-testid="quick-cred-role-input"
                    type="text"
                    :placeholder="t('tryit.quickRolePlaceholder')"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                  <div class="mt-1.5 flex flex-wrap gap-1">
                    <button
                      v-for="rolePreset in ROLE_PRESETS"
                      :key="rolePreset"
                      type="button"
                      class="rounded px-1.5 py-0.5 font-mono text-[10px] font-medium transition"
                      :class="
                        quickCredRole === rolePreset
                          ? 'bg-indigo-600 text-white'
                          : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50'
                      "
                      @click="quickCredRole = rolePreset"
                    >
                      {{ rolePreset }}
                    </button>
                  </div>
                </div>
              </div>

              <div>
                <span class="mb-1 block text-[11px] font-medium text-slate-700">
                  {{ t("tryit.quickPresetLabel") }}
                </span>
                <div class="flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    class="rounded-md px-2.5 py-1 text-[11px] font-medium transition"
                    :class="
                      quickCredPreset === 'bearer'
                        ? 'bg-indigo-600 text-white shadow-sm'
                        : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50'
                    "
                    @click="applyQuickPreset('bearer')"
                  >
                    {{ t("tryit.quickPresetBearer") }}
                  </button>
                  <button
                    type="button"
                    class="rounded-md px-2.5 py-1 text-[11px] font-medium transition"
                    :class="
                      quickCredPreset === 'cookie'
                        ? 'bg-indigo-600 text-white shadow-sm'
                        : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50'
                    "
                    @click="applyQuickPreset('cookie')"
                  >
                    {{ t("tryit.quickPresetCookie") }}
                  </button>
                  <button
                    type="button"
                    class="rounded-md px-2.5 py-1 text-[11px] font-medium transition"
                    :class="
                      quickCredPreset === 'custom'
                        ? 'bg-indigo-600 text-white shadow-sm'
                        : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50'
                    "
                    @click="applyQuickPreset('custom')"
                  >
                    {{ t("tryit.quickPresetCustom") }}
                  </button>
                </div>
              </div>

              <div class="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                <div>
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("tryit.quickHeaderName") }}
                  </label>
                  <input
                    v-model="quickCredHeaderName"
                    data-testid="quick-cred-header-name"
                    type="text"
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
                <div class="sm:col-span-2">
                  <label class="mb-1 block text-[11px] font-medium text-slate-700">
                    {{ t("tryit.quickHeaderValue") }}
                  </label>
                  <input
                    v-model="quickCredHeaderValue"
                    data-testid="quick-cred-header-value"
                    type="password"
                    autocomplete="off"
                    :placeholder="
                      quickCredPreset === 'cookie'
                        ? 'session_id=...; token=...'
                        : quickCredPreset === 'bearer'
                          ? 'Bearer eyJ... or ENV:RECONQA_ACCOUNT_A_TOKEN'
                          : 'token-value or ENV:API_KEY'
                    "
                    class="input-field !py-1.5 font-mono text-xs"
                  />
                </div>
              </div>

              <p class="text-[11px] leading-relaxed text-indigo-900/75">
                {{ t("tryit.quickValueHint") }}
              </p>

              <div class="flex justify-end gap-2">
                <button
                  type="button"
                  class="btn-secondary !py-1 !text-xs"
                  @click="showQuickAddCred = false"
                >
                  {{ t("common.cancel") }}
                </button>
                <button
                  type="button"
                  data-testid="quick-cred-save-btn"
                  class="btn-primary !py-1 !text-xs"
                  :disabled="
                    quickCredSaving ||
                    !quickCredName.trim() ||
                    !quickCredHeaderName.trim() ||
                    !quickCredHeaderValue.trim()
                  "
                  @click="handleQuickAddCredential"
                >
                  <Loader2 v-if="quickCredSaving" class="h-3 w-3 animate-spin" />
                  <Plus v-else class="h-3 w-3" />
                  <span>{{ quickCredSaving ? t("tryit.quickSavingBtn") : t("tryit.quickSaveBtn") }}</span>
                </button>
              </div>
            </div>
          </div>

          <!-- Query Params -->
          <div class="rounded-xl border border-slate-200 bg-slate-50/50 p-3.5">
            <div class="mb-2 flex items-center justify-between">
              <span class="text-xs font-semibold text-slate-700">
                {{ t("tryit.queryParamsTitle") }}
              </span>
              <button
                type="button"
                class="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800"
                @click="queryRows.push({ key: '', value: '' })"
              >
                <Plus class="h-3 w-3" />
                <span>{{ t("tryit.addRow") }}</span>
              </button>
            </div>
            <div v-if="queryRows.length > 0" class="space-y-1.5">
              <div v-for="(row, idx) in queryRows" :key="`q-${idx}`" class="flex items-center gap-2">
                <input
                  v-model="row.key"
                  type="text"
                  :placeholder="t('tryit.keyPlaceholder')"
                  class="input-field py-1 font-mono text-xs"
                />
                <input
                  v-model="row.value"
                  type="text"
                  :placeholder="t('tryit.valPlaceholder')"
                  class="input-field py-1 font-mono text-xs"
                />
                <button
                  type="button"
                  class="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                  @click="queryRows.splice(idx, 1)"
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>

          <!-- Custom Headers -->
          <div class="rounded-xl border border-slate-200 bg-slate-50/50 p-3.5">
            <div class="mb-2 flex items-center justify-between">
              <span class="text-xs font-semibold text-slate-700">
                {{ t("tryit.headersTitle") }}
              </span>
              <button
                type="button"
                class="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800"
                @click="headerRows.push({ key: '', value: '' })"
              >
                <Plus class="h-3 w-3" />
                <span>{{ t("tryit.addRow") }}</span>
              </button>
            </div>
            <div class="space-y-1.5">
              <div v-for="(row, idx) in headerRows" :key="`h-${idx}`" class="flex items-center gap-2">
                <input
                  v-model="row.key"
                  type="text"
                  :placeholder="t('tryit.keyPlaceholder')"
                  class="input-field py-1 font-mono text-xs"
                />
                <input
                  v-model="row.value"
                  type="text"
                  :placeholder="t('tryit.valPlaceholder')"
                  class="input-field py-1 font-mono text-xs"
                />
                <button
                  type="button"
                  class="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                  @click="headerRows.splice(idx, 1)"
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>

          <!-- Request Body -->
          <div v-if="bodySupported" class="space-y-1.5">
            <div class="flex items-center justify-between">
              <label class="text-xs font-semibold text-slate-700">
                {{ t("tryit.bodyTitle") }}
              </label>
              <button
                type="button"
                class="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800"
                @click="formatJsonBody"
              >
                <Sparkles class="h-3 w-3" />
                <span>{{ t("tryit.formatJson") }}</span>
              </button>
            </div>
            <textarea
              v-model="bodyText"
              data-testid="try-it-body"
              rows="6"
              spellcheck="false"
              :placeholder="t('tryit.bodyPlaceholder')"
              class="w-full rounded-xl border border-slate-300 bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-100 placeholder:text-slate-500 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
            />
          </div>

          <!-- Scenario Assertions Toggle -->
          <div
            v-if="activeAssertions.length > 0"
            data-testid="try-it-assertions-panel"
            class="rounded-xl border border-teal-200 bg-teal-50/60 p-3.5"
          >
            <label class="flex cursor-pointer items-center justify-between gap-2">
              <span class="inline-flex items-center gap-2 text-xs font-semibold text-teal-900">
                <input
                  v-model="runAssertions"
                  data-testid="try-it-assertions-toggle"
                  type="checkbox"
                  class="h-3.5 w-3.5 rounded border-teal-300 text-teal-600 focus:ring-teal-500"
                />
                <ListChecks class="h-3.5 w-3.5 text-teal-600" />
                <span>{{ t("tryit.assertionsToggle", { count: activeAssertions.length }) }}</span>
              </span>
            </label>
            <div v-if="runAssertions" class="mt-2 flex flex-wrap gap-1.5">
              <span
                v-for="asrt in activeAssertions"
                :key="asrt.id || asrt.title"
                class="inline-flex items-center gap-1 rounded-md border border-teal-200 bg-white px-2 py-0.5 text-[11px] text-slate-700"
              >
                <span class="font-medium text-slate-800">{{ asrt.title }}</span>
                <span class="rounded bg-teal-50 px-1 font-mono text-[10px] font-semibold text-teal-700">
                  {{ asrt.operator }}
                </span>
              </span>
            </div>
            <label
              v-if="projectId"
              class="mt-2.5 flex cursor-pointer items-center gap-2 border-t border-teal-200/70 pt-2 text-xs font-medium text-teal-900"
              :class="{ 'cursor-not-allowed opacity-50': !runAssertions }"
            >
              <input
                v-model="syncScenarios"
                data-testid="try-it-sync-scenarios-toggle"
                type="checkbox"
                :disabled="!runAssertions"
                class="h-3.5 w-3.5 rounded border-teal-300 text-teal-600 focus:ring-teal-500 disabled:cursor-not-allowed"
              />
              <span>{{ t("tryit.syncScenariosToggle") }}</span>
            </label>
          </div>
          <div
            v-else-if="projectId"
            data-testid="try-it-sync-scenarios-panel"
            class="rounded-xl border border-teal-200/80 bg-teal-50/40 px-3.5 py-2.5"
          >
            <label class="flex cursor-pointer items-center gap-2 text-xs font-medium text-teal-900">
              <input
                v-model="syncScenarios"
                data-testid="try-it-sync-scenarios-toggle"
                type="checkbox"
                class="h-3.5 w-3.5 rounded border-teal-300 text-teal-600 focus:ring-teal-500"
              />
              <ListChecks class="h-3.5 w-3.5 text-teal-600" />
              <span>{{ t("tryit.syncScenariosToggle") }}</span>
            </label>
          </div>

          <!-- Error Notice -->
          <div
            v-if="errorMsg"
            class="flex items-start gap-2 rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800"
          >
            <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0 text-rose-600" />
            <span>{{ errorMsg }}</span>
          </div>

          <!-- Multi-Role Authorization Matrix Results -->
          <div
            v-if="matrixResponse"
            data-testid="try-it-matrix-results"
            class="overflow-hidden rounded-xl border border-indigo-200 bg-white shadow-sm"
          >
            <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 bg-indigo-50/50 px-4 py-2.5">
              <div class="flex items-center gap-2">
                <Users class="h-4 w-4 text-indigo-600" />
                <span class="text-xs font-semibold text-slate-900">
                  {{ t("tryit.matrixTitle") }}
                </span>
              </div>
              <span class="font-mono text-[11px] text-slate-600">
                {{
                  t("tryit.matrixSummary", {
                    allowed: matrixResponse.allowed_count,
                    denied: matrixResponse.denied_count,
                    total: matrixResponse.results.length,
                  })
                }}
              </span>
            </div>

            <!-- Potential BFLA / IDOR Warning Banner -->
            <div
              v-if="matrixResponse.potential_bfla_warning"
              data-testid="try-it-matrix-bfla-warn"
              class="flex items-start gap-2 border-b border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-900"
            >
              <ShieldAlert class="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              <span class="font-medium">{{ t("tryit.matrixBflaWarn") }}</span>
            </div>

            <div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-slate-200 text-left text-xs">
                <thead class="bg-slate-50 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  <tr>
                    <th class="w-7 px-2 py-2" />
                    <th class="px-3 py-2">{{ t("tryit.matrixColRole") }}</th>
                    <th class="px-3 py-2">{{ t("tryit.matrixColCred") }}</th>
                    <th class="px-3 py-2">{{ t("tryit.matrixColHttp") }}</th>
                    <th class="px-3 py-2">{{ t("tryit.matrixColBiz") }}</th>
                    <th class="px-3 py-2 text-right">{{ t("tryit.matrixColLatency") }}</th>
                    <th class="px-3 py-2 text-right">{{ t("tryit.matrixColSize") }}</th>
                    <th class="px-3 py-2 text-right">{{ t("tryit.matrixColVerdict") }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-slate-100">
                  <template v-for="(entry, idx) in matrixResponse.results" :key="`${entry.credential_id ?? 'anon'}-${idx}`">
                    <tr
                      class="cursor-pointer transition hover:bg-slate-50"
                      @click="toggleMatrixRow(idx)"
                    >
                      <td class="px-2 py-2 text-slate-400">
                        <ChevronDown v-if="expandedMatrixRows.has(idx)" class="h-3.5 w-3.5 text-indigo-600" />
                        <ChevronRight v-else class="h-3.5 w-3.5" />
                      </td>
                      <td class="px-3 py-2">
                        <span class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-slate-700">
                          {{ entry.role_tag }}
                        </span>
                      </td>
                      <td class="px-3 py-2 font-medium text-slate-800">
                        {{ entry.credential_name }}
                      </td>
                      <td class="px-3 py-2">
                        <span
                          class="inline-flex rounded px-1.5 py-0.5 font-mono text-[11px] font-bold ring-1 ring-inset"
                          :class="statusColorClass(entry.status_code)"
                        >
                          {{ entry.status_code }}
                        </span>
                      </td>
                      <td class="px-3 py-2 font-mono text-slate-600">
                        {{ entry.biz_code !== null && entry.biz_code !== undefined ? entry.biz_code : "—" }}
                      </td>
                      <td class="px-3 py-2 text-right font-mono text-slate-500">
                        {{ Math.round(entry.duration_ms) }}ms
                      </td>
                      <td class="px-3 py-2 text-right font-mono text-slate-500">
                        {{ formatByteSize(entry.body_size) }}
                      </td>
                      <td class="px-3 py-2 text-right">
                        <span
                          class="inline-flex rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ring-inset"
                          :class="verdictBadgeClass(entry.verdict)"
                        >
                          {{ t(`tryit.verdict.${entry.verdict}`) }}
                        </span>
                      </td>
                    </tr>
                    <tr v-if="expandedMatrixRows.has(idx)" class="bg-slate-50/80">
                      <td colspan="8" class="px-4 py-2.5">
                        <p v-if="entry.error" class="mb-1.5 text-xs text-rose-700">{{ entry.error }}</p>
                        <pre
                          class="max-h-40 overflow-auto rounded-lg bg-slate-950 p-2.5 font-mono text-[11px] leading-relaxed text-slate-200"
                        >{{ entry.body_preview || "(empty body)" }}</pre>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Live Response Section -->
          <div
            class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm"
            data-testid="try-it-response"
          >
            <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 bg-slate-50/80 px-4 py-2.5">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-xs font-semibold text-slate-800">
                  {{ t("tryit.responseTitle") }}
                </span>
                <template v-if="response">
                  <span
                    class="inline-flex rounded-full px-2 py-0.5 font-mono text-[11px] font-bold ring-1 ring-inset"
                    :class="statusColorClass(response.status_code)"
                    data-testid="try-it-status"
                  >
                    {{ response.status_code }} {{ response.status_text }}
                  </span>
                  <span class="inline-flex items-center gap-1 font-mono text-[11px] text-slate-500">
                    <Clock class="h-3 w-3" />
                    {{ response.duration_ms }} ms
                  </span>
                  <span v-if="response.content_type" class="font-mono text-[10px] text-slate-400">
                    {{ response.content_type }}
                  </span>
                </template>
              </div>

              <div v-if="response" class="flex items-center gap-1.5">
                <div class="inline-flex rounded-md bg-slate-200/70 p-0.5 text-[11px]">
                  <button
                    type="button"
                    class="rounded px-2 py-0.5 font-medium transition"
                    :class="
                      respTab === 'body' ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-600'
                    "
                    @click="respTab = 'body'"
                  >
                    {{ t("tryit.respTabBody") }}
                  </button>
                  <button
                    type="button"
                    class="rounded px-2 py-0.5 font-medium transition"
                    :class="
                      respTab === 'headers'
                        ? 'bg-white text-indigo-700 shadow-sm'
                        : 'text-slate-600'
                    "
                    @click="respTab = 'headers'"
                  >
                    {{ t("tryit.respTabHeaders", { count: responseHeaderEntries.length }) }}
                  </button>
                </div>
                <button
                  v-if="respTab === 'body' && formattedResponseBody"
                  type="button"
                  class="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-600 hover:bg-slate-50"
                  @click="copyResponseBody"
                >
                  <Check v-if="copiedResp" class="h-3 w-3 text-emerald-600" />
                  <Copy v-else class="h-3 w-3" />
                  <span>{{ copiedResp ? t("tryit.copiedResp") : t("tryit.copyResp") }}</span>
                </button>
              </div>
            </div>

            <div v-if="!response" class="px-4 py-8 text-center text-xs text-slate-400">
              {{ t("tryit.emptyResp") }}
            </div>

            <div v-else class="p-4">
              <div
                v-if="response.error"
                class="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700"
              >
                {{ response.error }}
              </div>
              <div
                v-if="response.truncated"
                class="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-800"
              >
                {{ t("tryit.truncatedWarn") }}
              </div>
              <div
                v-if="isHtmlResponse"
                data-testid="try-it-html-hint"
                class="mb-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900"
              >
                <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
                <span>{{ t("tryit.htmlResponseHint") }}</span>
              </div>

              <div
                v-if="hasSyncFeedback(response)"
                data-testid="try-it-synced-badge"
                class="mb-3 flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-900"
              >
                <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-600" />
                <span class="font-medium">
                  {{
                    t("tryit.syncedBadge", {
                      scenarios: response.synced_scenarios ?? 0,
                      assertions: response.synced_assertions ?? 0,
                    })
                  }}
                </span>
              </div>

              <!-- Scenario Assertion Evaluation Results -->
              <div
                v-if="assertionResults.length > 0"
                data-testid="try-it-assertion-results"
                class="mb-3 rounded-xl border border-slate-200 bg-slate-50/70 p-3"
              >
                <div class="mb-2 flex items-center justify-between gap-2">
                  <span class="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-800">
                    <ListChecks class="h-3.5 w-3.5 text-teal-600" />
                    <span>{{ t("tryit.assertionsResultsTitle") }}</span>
                  </span>
                  <span
                    class="rounded-full px-2 py-0.5 font-mono text-[11px] font-bold"
                    :class="
                      assertionPassCount === assertionResults.length
                        ? 'bg-emerald-100 text-emerald-800'
                        : 'bg-rose-100 text-rose-800'
                    "
                  >
                    {{
                      t("tryit.assertionsSummary", {
                        passed: assertionPassCount,
                        total: assertionResults.length,
                      })
                    }}
                  </span>
                </div>

                <ul class="space-y-1.5">
                  <li
                    v-for="res in assertionResults"
                    :key="res.id || `${res.title}-${res.operator}`"
                    data-testid="try-it-assertion-result-item"
                    class="rounded-lg border px-2.5 py-2 text-xs"
                    :class="
                      res.passed
                        ? 'border-emerald-200 bg-emerald-50/60 text-emerald-950'
                        : 'border-rose-200 bg-rose-50/70 text-rose-950'
                    "
                  >
                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <div class="flex flex-wrap items-center gap-1.5">
                        <CheckCircle2 v-if="res.passed" class="h-3.5 w-3.5 shrink-0 text-emerald-600" />
                        <XCircle v-else class="h-3.5 w-3.5 shrink-0 text-rose-600" />
                        <span class="font-semibold">{{ res.title || res.operator }}</span>
                        <span class="rounded bg-white/80 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-slate-700 ring-1 ring-slate-200">
                          {{ res.operator }}
                        </span>
                      </div>
                      <span
                        class="rounded-full px-1.5 py-0.5 text-[10px] font-bold"
                        :class="
                          res.passed
                            ? 'bg-emerald-600 text-white'
                            : 'bg-rose-600 text-white'
                        "
                      >
                        {{ res.passed ? t("tryit.assertionsPassed") : t("tryit.assertionsFailed") }}
                      </span>
                    </div>
                    <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] opacity-90">
                      <span v-if="res.actual !== undefined">
                        {{ t("tryit.assertionsActual") }}: <strong>{{ formatAssertionVal(res.actual) }}</strong>
                      </span>
                      <span v-if="res.expected">
                        {{ t("tryit.assertionsExpected") }}: <strong>{{ res.expected }}</strong>
                      </span>
                    </div>
                    <p v-if="res.message" class="mt-1 text-[11px] opacity-85">
                      {{ res.message }}
                    </p>
                  </li>
                </ul>
              </div>

              <pre
                v-if="respTab === 'body'"
                data-testid="try-it-response-body"
                class="max-h-72 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-emerald-200"
              >{{ formattedResponseBody || "(empty body)" }}</pre>

              <div v-else class="max-h-64 overflow-auto">
                <dl class="divide-y divide-slate-100 text-xs">
                  <div
                    v-for="[hk, hv] in responseHeaderEntries"
                    :key="hk"
                    class="flex items-start justify-between gap-3 py-1.5"
                  >
                    <dt class="shrink-0 font-mono font-semibold text-slate-700">{{ hk }}</dt>
                    <dd class="break-all font-mono text-slate-500">{{ hv }}</dd>
                  </div>
                </dl>
              </div>
            </div>
          </div>
        </div>

        <!-- Footer -->
        <div class="flex flex-wrap items-center justify-end gap-2.5 border-t border-slate-200 bg-slate-50 px-6 py-3.5">
          <button type="button" class="btn-secondary" @click="emit('close')">
            {{ t("common.cancel") }}
          </button>
          <button
            v-if="projectId"
            type="button"
            data-testid="try-it-matrix-btn"
            class="btn-secondary border-indigo-200 bg-indigo-50/70 text-indigo-800 hover:bg-indigo-100"
            :disabled="runningMatrix || sending"
            @click="handleRunRoleMatrix"
          >
            <Loader2 v-if="runningMatrix" class="h-4 w-4 animate-spin text-indigo-600" />
            <ShieldAlert v-else class="h-4 w-4 text-indigo-600" />
            <span>{{ runningMatrix ? t("tryit.matrixRunning") : t("tryit.matrixBtn") }}</span>
          </button>
          <button
            type="button"
            data-testid="try-it-send-btn"
            class="btn-primary px-5"
            :disabled="sending || runningMatrix"
            @click="handleSend"
          >
            <Loader2 v-if="sending" class="h-4 w-4 animate-spin" />
            <Send v-else class="h-4 w-4" />
            <span>{{ sending ? t("tryit.sendingBtn") : t("tryit.sendBtn") }}</span>
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
