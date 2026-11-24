<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  AlertTriangle,
  Check,
  Clock,
  Copy,
  KeyRound,
  Loader2,
  Plus,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-vue-next";
import { listAuthProfiles, listTargets, replayEndpoint } from "@/api/projects";
import type { AuthProfile, ReplayResponse } from "@/api/types";
import { useI18n } from "@/i18n";

export interface TryItEndpointInput {
  method: string;
  path: string;
  host?: string;
  baseUrl?: string;
  summary?: string;
  parameters?: Array<Record<string, unknown>>;
  requestExample?: unknown;
}

interface KvRow {
  key: string;
  value: string;
}

const props = defineProps<{
  open: boolean;
  projectId: string;
  endpoint: TryItEndpointInput | null;
}>();

const emit = defineEmits<{
  close: [];
}>();

const { t } = useI18n();

const HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] as const;

const method = ref("GET");
const targetUrl = ref("");
const credentialId = ref("");
const timeoutSec = ref(15);
const queryRows = ref<KvRow[]>([]);
const headerRows = ref<KvRow[]>([{ key: "Accept", value: "application/json" }]);
const bodyText = ref("");

const authProfiles = ref<AuthProfile[]>([]);
const defaultBaseUrl = ref("");
const sending = ref(false);
const errorMsg = ref<string | null>(null);
const response = ref<ReplayResponse | null>(null);
const respTab = ref<"body" | "headers">("body");
const copiedResp = ref(false);

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
  if (ep.host) {
    const cleanHost = ep.host.replace(/^https?:\/\//i, "").replace(/\/+$/, "");
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
    return;
  }
  const [profilesRes, targetsRes] = await Promise.allSettled([
    listAuthProfiles(pid),
    listTargets(pid),
  ]);
  if (profilesRes.status === "fulfilled") {
    authProfiles.value = profilesRes.value;
  }
  if (targetsRes.status === "fulfilled" && targetsRes.value.length > 0) {
    defaultBaseUrl.value = targetsRes.value[0].base_url || "";
  }
}

async function hydrateFromEndpoint(): Promise<void> {
  if (!props.open) return;
  response.value = null;
  errorMsg.value = null;
  respTab.value = "body";

  await loadProjectContext(props.projectId);

  const ep = props.endpoint;
  if (!ep) return;

  method.value = (ep.method || "GET").toUpperCase();
  targetUrl.value = buildPrefilledUrl(ep, defaultBaseUrl.value);

  // Extract query parameters from OpenAPI parameters list
  const qRows: KvRow[] = [];
  const hRows: KvRow[] = [{ key: "Accept", value: "application/json" }];
  for (const param of ep.parameters ?? []) {
    const pIn = String(param.in ?? "").toLowerCase();
    const pName = String(param.name ?? "");
    const pVal = param.example !== undefined && param.example !== null ? String(param.example) : "";
    if (!pName) continue;
    if (pIn === "query") {
      qRows.push({ key: pName, value: pVal });
    } else if (pIn === "header" && pName.toLowerCase() !== "accept") {
      hRows.push({ key: pName, value: pVal });
    }
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
    response.value = await replayEndpoint(props.projectId, {
      method: method.value,
      url,
      headers,
      query_params: queryParams,
      body: parsedBody,
      credential_id: credentialId.value || null,
      timeout_sec: timeoutSec.value,
    });
    respTab.value = "body";
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t("tryit.errSend");
  } finally {
    sending.value = false;
  }
}

async function copyResponseBody(): Promise<void> {
  if (!formattedResponseBody.value) return;
  try {
    await navigator.clipboard.writeText(formattedResponseBody.value);
    copiedResp.value = true;
    window.setTimeout(() => {
      copiedResp.value = false;
    }, 1800);
  } catch {
    copiedResp.value = true;
  }
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
                <label class="mb-1 flex items-center gap-1 text-xs font-medium text-slate-700">
                  <KeyRound class="h-3.5 w-3.5 text-indigo-500" />
                  <span>{{ t("tryit.credentialLabel") }}</span>
                </label>
                <select
                  v-model="credentialId"
                  data-testid="try-it-credential"
                  class="input-field py-1.5 text-xs"
                >
                  <option value="">{{ t("tryit.credentialNone") }}</option>
                  <option v-for="cred in authProfiles" :key="cred.id" :value="cred.id">
                    {{ cred.name }} ({{ cred.role_tag }})
                  </option>
                </select>
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

          <!-- Error Notice -->
          <div
            v-if="errorMsg"
            class="flex items-start gap-2 rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800"
          >
            <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0 text-rose-600" />
            <span>{{ errorMsg }}</span>
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
        <div class="flex items-center justify-end gap-2.5 border-t border-slate-200 bg-slate-50 px-6 py-3.5">
          <button type="button" class="btn-secondary" @click="emit('close')">
            {{ t("common.cancel") }}
          </button>
          <button
            type="button"
            data-testid="try-it-send-btn"
            class="btn-primary px-5"
            :disabled="sending"
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
