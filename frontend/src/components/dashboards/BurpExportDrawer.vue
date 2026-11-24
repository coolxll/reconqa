<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  Check,
  Copy,
  Download,
  FileCode2,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Terminal,
  X,
} from "lucide-vue-next";
import { artifactDownloadUrl } from "@/api/client";
import { triggerBurpExport } from "@/api/runs";
import type { BurpExportResponse } from "@/api/types";
import { useI18n } from "@/i18n";
import "@/i18n/locales/dashboards";

interface ExportedItem {
  index: number;
  method: string;
  url: string;
  path: string;
  reasons: string[];
  filename: string;
}

const props = defineProps<{
  runId: string;
  endpoints: string[];
  open: boolean;
}>();

const emit = defineEmits<{
  (e: "close"): void;
}>();

const { t } = useI18n();

const loading = ref(false);
const error = ref<string | null>(null);
const targetDomain = ref("");
const customEndpointInput = ref("");
const selectedEndpoints = ref<string[]>([]);
const exportResp = ref<BurpExportResponse | null>(null);
const copiedKey = ref<string | null>(null);
const activePreviewTab = ref<"raw" | "curl">("raw");

const downloadZipUrl = computed(() => artifactDownloadUrl(props.runId, "burp_exports"));

const exportedItems = computed<ExportedItem[]>(() => {
  const raw = exportResp.value?.burp_export;
  if (!raw) return [];
  const list = raw.exported;
  if (Array.isArray(list)) {
    return list.map((item, idx) => {
      const obj = (item ?? {}) as Record<string, unknown>;
      return {
        index: typeof obj.index === "number" ? obj.index : idx + 1,
        method: String(obj.method ?? "GET").toUpperCase(),
        url: String(obj.url ?? ""),
        path: String(obj.path ?? "/"),
        reasons: Array.isArray(obj.reasons) ? obj.reasons.map(String) : [],
        filename: String(obj.filename ?? `${String(idx + 1).padStart(3, "0")}_request.http`),
      };
    });
  }
  const files = raw.files;
  if (Array.isArray(files)) {
    return files.map((file, idx) => ({
      index: idx + 1,
      method: "HTTP",
      url: "",
      path: String(file),
      reasons: ["exported"],
      filename: String(file),
    }));
  }
  return [];
});

const exportedCount = computed(() => {
  const raw = exportResp.value?.burp_export;
  if (!raw) return 0;
  if (typeof raw.count === "number") return raw.count;
  if (typeof raw.exported_count === "number") return raw.exported_count;
  return exportedItems.value.length;
});

const rawHttpPreview = computed(() => {
  if (exportedItems.value.length === 0) {
    return [t("burp.noMatchComment1"), t("burp.noMatchComment2")].join("\n");
  }
  return exportedItems.value
    .slice(0, 6)
    .map((item) => {
      let host = "api.example.com";
      let pathWithQuery = item.path || "/";
      try {
        if (item.url) {
          const parsed = new URL(item.url);
          host = parsed.host || host;
          pathWithQuery = `${parsed.pathname}${parsed.search}` || "/";
        }
      } catch {
        // Keep fallback host/path
      }
      const reasonLine = item.reasons.length ? `# Reasons: ${item.reasons.join(", ")}` : "";
      return [
        `# [${String(item.index).padStart(3, "0")}] ${item.filename}`,
        reasonLine,
        `${item.method} ${pathWithQuery} HTTP/1.1`,
        `Host: ${host}`,
        "Accept: application/json",
        t("burp.previewHeadersNote"),
      ]
        .filter(Boolean)
        .join("\n");
    })
    .join("\n\n");
});

const curlPreview = computed(() => {
  const lines = [
    t("burp.curlStep1"),
    `curl -sSL "${downloadZipUrl.value}" -o burp_exports.zip && unzip -o burp_exports.zip -d burp_exports`,
    "",
    t("burp.curlStep2"),
    "chmod +x burp_exports/replay_commands.sh && ./burp_exports/replay_commands.sh",
  ];
  if (exportedItems.value.length > 0) {
    lines.push("", t("burp.curlQuickTitle"));
    for (const item of exportedItems.value.slice(0, 5)) {
      const targetUrl = item.url || `https://api.example.com${item.path}`;
      lines.push(
        `# [${String(item.index).padStart(3, "0")}] ${item.filename}`,
        `curl -i -sS -X ${item.method} '${targetUrl}'`
      );
    }
  }
  return lines.join("\n");
});

function syncEndpointsFromProps(): void {
  selectedEndpoints.value = Array.from(
    new Set((props.endpoints || []).map((ep) => ep.trim()).filter(Boolean))
  );
}

function addCustomEndpoint(): void {
  const trimmed = customEndpointInput.value.trim();
  if (!trimmed) return;
  if (!selectedEndpoints.value.includes(trimmed)) {
    selectedEndpoints.value.push(trimmed);
  }
  customEndpointInput.value = "";
}

function removeEndpoint(ep: string): void {
  selectedEndpoints.value = selectedEndpoints.value.filter((item) => item !== ep);
}

async function runExport(): Promise<void> {
  if (!props.runId) return;
  loading.value = true;
  error.value = null;
  try {
    exportResp.value = await triggerBurpExport(props.runId, {
      endpoints: selectedEndpoints.value.length > 0 ? selectedEndpoints.value : null,
      target_domain: targetDomain.value.trim() || null,
    });
  } catch (err) {
    error.value = err instanceof Error ? err.message : t("burp.errorExport");
  } finally {
    loading.value = false;
  }
}

async function copyText(key: string, text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    copiedKey.value = key;
    window.setTimeout(() => {
      if (copiedKey.value === key) copiedKey.value = null;
    }, 1800);
  } catch {
    // Clipboard unavailable in insecure context
  }
}

function onKeyDown(ev: KeyboardEvent): void {
  if (ev.key === "Escape" && props.open) {
    emit("close");
  }
}

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      syncEndpointsFromProps();
      window.addEventListener("keydown", onKeyDown);
      void runExport();
    } else {
      window.removeEventListener("keydown", onKeyDown);
    }
  },
  { immediate: true }
);

watch(
  () => props.endpoints,
  () => {
    if (props.open) {
      syncEndpointsFromProps();
    }
  }
);

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeyDown);
});
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex justify-end bg-slate-900/45 backdrop-blur-[1px]"
      @click.self="emit('close')"
    >
      <div
        class="flex h-full w-full max-w-2xl flex-col bg-white shadow-2xl ring-1 ring-slate-200"
        role="dialog"
        aria-modal="true"
        :aria-label="t('burp.ariaLabel')"
      >
        <!-- Header -->
        <div class="flex items-center justify-between border-b border-slate-200 px-6 py-4">
          <div class="flex items-center gap-3">
            <div
              class="flex h-9 w-9 items-center justify-center rounded-lg bg-amber-50 text-amber-600 ring-1 ring-amber-200"
            >
              <ShieldAlert class="h-5 w-5" />
            </div>
            <div>
              <h2 class="text-base font-semibold text-slate-900">
                {{ t("burp.title") }}
              </h2>
              <p class="text-xs text-slate-500">
                {{ t("burp.subtitle") }}
              </p>
            </div>
          </div>
          <button
            type="button"
            class="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
            :aria-label="t('burp.closeAria')"
            @click="emit('close')"
          >
            <X class="h-5 w-5" />
          </button>
        </div>

        <!-- Body -->
        <div class="flex-1 space-y-5 overflow-y-auto px-6 py-5">
          <!-- Selected endpoints & filter controls -->
          <div class="rounded-xl border border-slate-200 bg-slate-50/70 p-4">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="text-xs font-semibold uppercase tracking-wider text-slate-600">
                {{ t("burp.targetFilterTitle") }}
              </p>
              <span class="text-xs text-slate-500">
                {{
                  selectedEndpoints.length > 0
                    ? t("burp.selectedCount", { count: selectedEndpoints.length })
                    : t("burp.autoSelectHint")
                }}
              </span>
            </div>

            <div v-if="selectedEndpoints.length > 0" class="mt-2.5 flex flex-wrap gap-1.5">
              <span
                v-for="ep in selectedEndpoints"
                :key="ep"
                class="inline-flex items-center gap-1 rounded-md bg-white px-2 py-1 font-mono text-xs text-slate-700 ring-1 ring-slate-200"
              >
                <span class="max-w-[340px] truncate">{{ ep }}</span>
                <button
                  type="button"
                  class="text-slate-400 hover:text-rose-600"
                  :aria-label="t('burp.removeEndpointAria')"
                  @click="removeEndpoint(ep)"
                >
                  <X class="h-3.5 w-3.5" />
                </button>
              </span>
            </div>

            <div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <div class="flex gap-1.5 sm:col-span-2">
                <input
                  v-model="customEndpointInput"
                  type="text"
                  class="input-field py-1.5 text-xs font-mono"
                  :placeholder="t('burp.addEndpointPlaceholder')"
                  @keyup.enter="addCustomEndpoint"
                />
                <button
                  type="button"
                  class="btn-secondary shrink-0 px-2.5 py-1.5 text-xs"
                  @click="addCustomEndpoint"
                >
                  {{ t("burp.addBtn") }}
                </button>
              </div>
              <input
                v-model="targetDomain"
                type="text"
                class="input-field py-1.5 text-xs font-mono"
                :placeholder="t('burp.domainPlaceholder')"
              />
            </div>

            <div class="mt-3 flex items-center justify-between gap-2">
              <button
                type="button"
                class="btn-primary px-3 py-1.5 text-xs"
                :disabled="loading"
                @click="runExport"
              >
                <Loader2 v-if="loading" class="h-3.5 w-3.5 animate-spin" />
                <RefreshCw v-else class="h-3.5 w-3.5" />
                <span>{{ loading ? t("burp.exporting") : t("burp.refreshExport") }}</span>
              </button>
              <a
                v-if="exportResp && exportedCount > 0"
                :href="downloadZipUrl"
                download="burp_exports.zip"
                class="btn-secondary px-3 py-1.5 text-xs text-indigo-700 ring-1 ring-indigo-200 hover:bg-indigo-50"
              >
                <Download class="h-3.5 w-3.5" />
                <span>{{ t("burp.downloadZip") }}</span>
              </a>
            </div>
          </div>

          <!-- Error banner -->
          <div
            v-if="error"
            class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"
          >
            {{ error }}
          </div>

          <!-- Loading state -->
          <div
            v-if="loading && !exportResp"
            class="flex items-center justify-center gap-2 py-10 text-sm text-slate-500"
          >
            <Loader2 class="h-4 w-4 animate-spin text-indigo-600" />
            <span>{{ t("burp.loadingPackets") }}</span>
          </div>

          <!-- Results -->
          <template v-else-if="exportResp">
            <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50/60 px-4 py-3">
              <div>
                <p class="text-sm font-semibold text-emerald-900">
                  {{ t("burp.readyTitle", { count: exportedCount }) }}
                </p>
                <p class="text-xs text-emerald-700">
                  {{ t("burp.readyDesc") }}
                </p>
              </div>
              <a
                :href="downloadZipUrl"
                download="burp_exports.zip"
                class="btn-primary bg-emerald-600 px-3 py-1.5 text-xs hover:bg-emerald-700"
              >
                <Download class="h-3.5 w-3.5" />
                <span>burp_exports.zip</span>
              </a>
            </div>

            <!-- Exported request list -->
            <div v-if="exportedItems.length > 0" class="card overflow-hidden">
              <div class="card-header py-2.5">
                <span class="text-xs font-semibold uppercase tracking-wider text-slate-600">
                  {{ t("burp.exportedPackets", { count: exportedItems.length }) }}
                </span>
              </div>
              <div class="max-h-56 divide-y divide-slate-100 overflow-y-auto">
                <div
                  v-for="item in exportedItems"
                  :key="item.filename"
                  class="flex items-start justify-between gap-3 px-4 py-2.5 text-xs"
                >
                  <div class="min-w-0">
                    <div class="flex items-center gap-2">
                      <span
                        class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-slate-700"
                      >
                        {{ item.method }}
                      </span>
                      <span class="truncate font-mono text-slate-800" :title="item.url || item.path">
                        {{ item.path || item.url }}
                      </span>
                    </div>
                    <p class="mt-1 font-mono text-[11px] text-slate-500">
                      {{ item.filename }}
                    </p>
                  </div>
                  <div class="flex shrink-0 flex-wrap justify-end gap-1">
                    <span
                      v-for="reason in item.reasons"
                      :key="reason"
                      class="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-800 ring-1 ring-amber-200"
                    >
                      {{ reason }}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <!-- Preview Tabs -->
            <div class="card overflow-hidden">
              <div class="flex items-center justify-between border-b border-slate-100 px-4 py-2.5">
                <div class="flex items-center gap-1">
                  <button
                    type="button"
                    class="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition"
                    :class="
                      activePreviewTab === 'raw'
                        ? 'bg-indigo-50 text-indigo-700'
                        : 'text-slate-600 hover:bg-slate-100'
                    "
                    @click="activePreviewTab = 'raw'"
                  >
                    <FileCode2 class="h-3.5 w-3.5" />
                    <span>{{ t("burp.tabRaw") }}</span>
                  </button>
                  <button
                    type="button"
                    class="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition"
                    :class="
                      activePreviewTab === 'curl'
                        ? 'bg-indigo-50 text-indigo-700'
                        : 'text-slate-600 hover:bg-slate-100'
                    "
                    @click="activePreviewTab = 'curl'"
                  >
                    <Terminal class="h-3.5 w-3.5" />
                    <span>{{ t("burp.tabCurl") }}</span>
                  </button>
                </div>
                <button
                  type="button"
                  class="btn-secondary px-2.5 py-1 text-xs"
                  @click="
                    copyText(
                      activePreviewTab,
                      activePreviewTab === 'raw' ? rawHttpPreview : curlPreview
                    )
                  "
                >
                  <Check v-if="copiedKey === activePreviewTab" class="h-3.5 w-3.5 text-emerald-600" />
                  <Copy v-else class="h-3.5 w-3.5" />
                  <span>{{ copiedKey === activePreviewTab ? t("burp.copied") : t("burp.copy") }}</span>
                </button>
              </div>
              <pre
                class="max-h-72 overflow-auto bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-100"
              >{{ activePreviewTab === "raw" ? rawHttpPreview : curlPreview }}</pre>
            </div>
          </template>
        </div>

        <!-- Footer -->
        <div class="flex items-center justify-between border-t border-slate-200 bg-slate-50 px-6 py-3">
          <p class="text-xs text-slate-500">
            {{ t("burp.footerTipPrefix") }}
            <strong>{{ t("burp.footerTipAction") }}</strong>
            {{ t("burp.footerTipSuffix") }}
          </p>
          <button type="button" class="btn-secondary px-3.5 py-1.5 text-xs" @click="emit('close')">
            {{ t("burp.close") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
