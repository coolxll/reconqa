<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Container,
  Copy,
  Download,
  Info,
  Loader2,
  RefreshCw,
  Terminal,
  Wrench,
  X,
  XCircle,
} from "lucide-vue-next";
import { getDoctorStatus, installDoctorTool } from "@/api/reports";
import type { DoctorInstallResult, DoctorToolStatus } from "@/api/types";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import { useI18n } from "@/i18n";
import { copyToClipboard } from "@/utils/clipboard";

interface DoctorEntry extends DoctorToolStatus {
  name: string;
}

const CORE_TOOL_ORDER = ["mvn", "allure", "k6", "mitmdump", "nuclei", "java", "docker"];
const CORE_TOOL_SET = new Set(CORE_TOOL_ORDER);

const { locale, t } = useI18n();

const doctorLoading = ref(true);
const doctorError = ref<string | null>(null);
const doctorTools = ref<Record<string, DoctorToolStatus>>({});

const selectedToolName = ref<string | null>(null);
const copiedField = ref<"install" | "docker" | null>(null);
const installingTool = ref(false);
const installResult = ref<DoctorInstallResult | null>(null);
const installError = ref<string | null>(null);

const doctorEntries = computed<DoctorEntry[]>(() =>
  Object.entries(doctorTools.value).map(([name, info]) => ({
    name,
    ...info,
  }))
);

const coreExecutionTools = computed<DoctorEntry[]>(() => {
  const list = doctorEntries.value.filter((item) => CORE_TOOL_SET.has(item.name));
  list.sort((a, b) => CORE_TOOL_ORDER.indexOf(a.name) - CORE_TOOL_ORDER.indexOf(b.name));
  return list;
});

const optionalDiscoveryTools = computed<DoctorEntry[]>(() =>
  doctorEntries.value.filter((item) => !CORE_TOOL_SET.has(item.name))
);

const doctorAvailableCount = computed(
  () => doctorEntries.value.filter((item) => item.available).length
);

const coreAvailableCount = computed(
  () => coreExecutionTools.value.filter((item) => item.available).length
);

const selectedTool = computed<DoctorEntry | null>(() => {
  if (!selectedToolName.value) return null;
  const info = doctorTools.value[selectedToolName.value];
  if (!info) return null;
  return { name: selectedToolName.value, ...info };
});

const selectedToolPurpose = computed(() => {
  const tool = selectedTool.value;
  if (!tool) return "";
  const localized = locale.value === "zh" ? tool.purpose_zh : tool.purpose_en;
  return (
    localized ||
    tool.purpose_en ||
    tool.purpose_zh ||
    t("overview.doctor.modal.defaultPurpose", { category: tool.category })
  );
});

const selectedToolImpact = computed(() => {
  const tool = selectedTool.value;
  if (!tool) return "";
  const localized = locale.value === "zh" ? tool.impact_zh : tool.impact_en;
  return (
    localized ||
    tool.impact_en ||
    tool.impact_zh ||
    t("overview.doctor.modal.defaultImpact")
  );
});

const selectedToolInstallCmd = computed(() => {
  const tool = selectedTool.value;
  if (!tool) return "";
  return tool.install_cmd || tool.install_hint || "";
});

function openToolModal(toolName: string): void {
  selectedToolName.value = toolName;
  copiedField.value = null;
  installResult.value = null;
  installError.value = null;
}

function closeToolModal(): void {
  if (installingTool.value) return;
  selectedToolName.value = null;
  copiedField.value = null;
  installResult.value = null;
  installError.value = null;
}

async function copyCommand(text: string, field: "install" | "docker"): Promise<void> {
  if (!text) return;
  const ok = await copyToClipboard(text);
  if (!ok) return;
  copiedField.value = field;
  window.setTimeout(() => {
    if (copiedField.value === field) copiedField.value = null;
  }, 2000);
}

async function handleAutoInstall(): Promise<void> {
  const tool = selectedTool.value;
  if (!tool || installingTool.value) return;
  installingTool.value = true;
  installResult.value = null;
  installError.value = null;
  try {
    const res = await installDoctorTool(tool.name);
    installResult.value = res;
    if (res.doctor && typeof res.doctor === "object") {
      doctorTools.value = res.doctor;
    } else {
      await loadDoctor();
    }
  } catch (err) {
    installError.value = err instanceof Error ? err.message : t("overview.err.doctor");
  } finally {
    installingTool.value = false;
  }
}

function onKeyDown(e: KeyboardEvent): void {
  if (e.key === "Escape" && selectedToolName.value) closeToolModal();
}

async function loadDoctor(): Promise<void> {
  doctorLoading.value = true;
  doctorError.value = null;
  try {
    doctorTools.value = await getDoctorStatus();
  } catch (err) {
    doctorError.value = err instanceof Error ? err.message : t("overview.err.doctor");
  } finally {
    doctorLoading.value = false;
  }
}

onMounted(() => {
  void loadDoctor();
  window.addEventListener("keydown", onKeyDown);
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeyDown);
});
</script>

<template>
  <div class="space-y-6">
    <section class="card overflow-hidden">
      <div class="card-header">
        <div class="flex items-center gap-2.5">
          <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
            <Wrench class="h-4 w-4" />
          </div>
          <div>
            <h2 class="text-sm font-semibold text-slate-900">{{ t("doctor.pageTitle") }}</h2>
            <p class="text-xs text-slate-500">
              {{ t("doctor.pageSubtitle") }} ·
              <code class="font-mono text-[11px]">GET /api/v1/system/doctor</code>
            </p>
          </div>
        </div>
        <div class="flex items-center gap-2.5">
          <span
            v-if="!doctorLoading && doctorEntries.length > 0"
            class="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700"
          >
            {{
              t("overview.doctor.summary", {
                coreReady: coreAvailableCount,
                coreTotal: coreExecutionTools.length,
                totalReady: doctorAvailableCount,
                total: doctorEntries.length,
              })
            }}
          </span>
          <button
            type="button"
            class="btn-secondary !px-2.5 !py-1.5 !text-xs"
            :disabled="doctorLoading"
            data-testid="doctor-recheck-btn"
            @click="loadDoctor"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="doctorLoading ? 'animate-spin' : ''" />
            <span>{{ t("overview.doctor.recheck") }}</span>
          </button>
        </div>
      </div>

      <!-- Reassuring banner -->
      <div
        class="flex items-start gap-2.5 border-b border-slate-100 bg-indigo-50/45 px-5 py-3 text-xs text-indigo-950"
      >
        <Info class="mt-0.5 h-4 w-4 shrink-0 text-indigo-600" />
        <span class="leading-relaxed">{{ t("overview.doctor.reassureBanner") }}</span>
      </div>

      <div v-if="doctorLoading" class="p-8 text-center text-sm text-slate-500">
        {{ t("overview.doctor.loading") }}
      </div>

      <div v-else-if="doctorError" class="p-5">
        <div class="flex items-center gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-800">
          <AlertTriangle class="h-4 w-4 shrink-0 text-amber-600" />
          <span>{{ doctorError }}</span>
        </div>
      </div>

      <div v-else-if="doctorEntries.length === 0" class="p-6 text-center text-xs text-slate-500">
        {{ t("overview.doctor.empty") }}
      </div>

      <div v-else class="space-y-6 p-5">
        <!-- Group 1: Core Execution -->
        <div>
          <div class="mb-3 flex items-center justify-between">
            <div>
              <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-700">
                {{ t("overview.doctor.coreTitle") }}
              </h3>
              <p class="text-[11px] text-slate-500">{{ t("overview.doctor.coreDesc") }}</p>
            </div>
            <span class="rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700">
              {{
                t("overview.doctor.coreReady", {
                  ready: coreAvailableCount,
                  total: coreExecutionTools.length,
                })
              }}
            </span>
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            <button
              v-for="tool in coreExecutionTools"
              :key="tool.name"
              type="button"
              class="group flex flex-col justify-between rounded-xl border p-3.5 text-left transition hover:border-indigo-300 hover:shadow-sm focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
              :class="
                tool.available
                  ? 'border-emerald-200/80 bg-emerald-50/25 hover:bg-emerald-50/45'
                  : 'border-slate-200 bg-slate-50/60 hover:bg-white'
              "
              :data-testid="`doctor-tool-${tool.name}`"
              @click="openToolModal(tool.name)"
            >
              <div class="flex w-full items-start justify-between gap-2">
                <div class="min-w-0">
                  <div class="flex items-center gap-1.5">
                    <CheckCircle2 v-if="tool.available" class="h-4 w-4 shrink-0 text-emerald-600" />
                    <XCircle v-else class="h-4 w-4 shrink-0 text-slate-400" />
                    <span class="truncate font-mono text-sm font-semibold text-slate-900">
                      {{ tool.name }}
                    </span>
                  </div>
                  <div class="mt-1 flex flex-wrap items-center gap-1">
                    <span
                      class="inline-block rounded bg-white/90 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-500 ring-1 ring-inset ring-slate-200"
                    >
                      {{ tool.category }}
                    </span>
                    <span
                      v-if="!tool.available && tool.auto_installable"
                      class="inline-flex items-center gap-0.5 rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700 ring-1 ring-inset ring-indigo-600/20"
                    >
                      <Download class="h-2.5 w-2.5" />
                      <span>{{ t("overview.doctor.autoBadge") }}</span>
                    </span>
                  </div>
                </div>
                <StatusBadge :status="tool.available ? 'available' : 'missing'" />
              </div>

              <div class="mt-3 flex w-full items-center justify-between gap-2 border-t border-slate-200/70 pt-2 text-[11px]">
                <div
                  v-if="tool.available && tool.path"
                  class="flex min-w-0 items-center gap-1.5 truncate font-mono text-slate-600"
                  :title="tool.path"
                >
                  <Terminal class="h-3 w-3 shrink-0 text-slate-400" />
                  <span class="truncate">{{ tool.path }}</span>
                </div>
                <div v-else class="min-w-0 truncate text-slate-500" :title="tool.install_hint">
                  {{ tool.install_hint }}
                </div>
                <span
                  class="shrink-0 font-medium transition"
                  :class="
                    tool.available
                      ? 'text-slate-400 group-hover:text-indigo-600'
                      : 'text-indigo-600 group-hover:text-indigo-800'
                  "
                >
                  {{
                    tool.available
                      ? t("overview.doctor.detailsAction")
                      : t("overview.doctor.setupAction")
                  }}
                  →
                </span>
              </div>
            </button>
          </div>
        </div>

        <!-- Group 2: Optional Helpers -->
        <div v-if="optionalDiscoveryTools.length > 0" class="border-t border-slate-100 pt-5">
          <div class="mb-3 flex items-center justify-between">
            <div>
              <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-500">
                {{ t("overview.doctor.optionalTitle") }}
              </h3>
              <p class="text-[11px] text-slate-400">{{ t("overview.doctor.optionalDesc") }}</p>
            </div>
            <span class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">
              {{ t("common.optional") }}
            </span>
          </div>

          <div class="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
            <button
              v-for="tool in optionalDiscoveryTools"
              :key="tool.name"
              type="button"
              class="group flex flex-col justify-between rounded-lg border border-slate-200/80 bg-slate-50/40 p-3 text-left transition hover:border-indigo-300 hover:bg-white hover:shadow-sm focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
              :data-testid="`doctor-tool-${tool.name}`"
              @click="openToolModal(tool.name)"
            >
              <div class="flex w-full items-center justify-between gap-2">
                <div class="flex min-w-0 items-center gap-1.5">
                  <CheckCircle2 v-if="tool.available" class="h-3.5 w-3.5 shrink-0 text-emerald-600" />
                  <XCircle v-else class="h-3.5 w-3.5 shrink-0 text-slate-400" />
                  <span class="truncate font-mono text-xs font-semibold text-slate-800">
                    {{ tool.name }}
                  </span>
                </div>
                <StatusBadge :status="tool.available ? 'available' : 'missing'" />
              </div>
              <div class="mt-2 flex w-full items-center justify-between gap-2 text-[10px]">
                <p
                  class="min-w-0 truncate font-mono text-slate-500"
                  :title="tool.available && tool.path ? tool.path : tool.install_hint"
                >
                  {{ tool.available && tool.path ? tool.path : tool.install_hint }}
                </p>
                <span
                  class="shrink-0 font-medium"
                  :class="
                    tool.available
                      ? 'text-slate-400 group-hover:text-indigo-600'
                      : 'text-indigo-600 group-hover:text-indigo-800'
                  "
                >
                  {{
                    tool.available
                      ? t("overview.doctor.detailsAction")
                      : t("overview.doctor.setupAction")
                  }}
                </span>
              </div>
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- Tool Setup Modal -->
    <div
      v-if="selectedTool"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
      data-testid="doctor-setup-modal"
      @click.self="closeToolModal"
    >
      <div class="w-full max-w-xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
        <div class="flex items-center justify-between gap-3 border-b border-slate-100 px-6 py-4">
          <div class="flex min-w-0 items-center gap-2.5">
            <div
              class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg"
              :class="
                selectedTool.available
                  ? 'bg-emerald-50 text-emerald-600'
                  : 'bg-indigo-50 text-indigo-600'
              "
            >
              <Wrench class="h-4 w-4" />
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <h3 class="font-mono text-base font-bold text-slate-900">{{ selectedTool.name }}</h3>
              <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-600">
                {{ selectedTool.category }}
              </span>
              <StatusBadge :status="selectedTool.available ? 'available' : 'missing'" />
            </div>
          </div>
          <button
            type="button"
            class="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 disabled:opacity-40"
            :disabled="installingTool"
            @click="closeToolModal"
          >
            <X class="h-4 w-4" />
          </button>
        </div>

        <div class="max-h-[75vh] space-y-4 overflow-y-auto px-6 py-5 text-sm">
          <div
            v-if="selectedTool.available && selectedTool.path"
            class="rounded-xl border border-emerald-200/80 bg-emerald-50/40 p-3.5"
          >
            <p class="text-[11px] font-semibold uppercase tracking-wider text-emerald-800">
              {{ t("overview.doctor.modal.path") }}
            </p>
            <p class="mt-1 break-all font-mono text-xs text-emerald-950">{{ selectedTool.path }}</p>
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div class="rounded-xl border border-slate-200/80 bg-slate-50/70 p-3.5">
              <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                {{ t("overview.doctor.modal.purpose") }}
              </p>
              <p class="mt-1.5 text-xs leading-relaxed text-slate-800">{{ selectedToolPurpose }}</p>
            </div>
            <div class="rounded-xl border border-slate-200/80 bg-slate-50/70 p-3.5">
              <p class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                {{ t("overview.doctor.modal.impact") }}
              </p>
              <p class="mt-1.5 text-xs leading-relaxed text-slate-700">{{ selectedToolImpact }}</p>
            </div>
          </div>

          <div v-if="selectedToolInstallCmd">
            <div class="mb-1.5 flex items-center justify-between">
              <span class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
                <Terminal class="h-3.5 w-3.5 text-slate-500" />
                <span>{{ t("overview.doctor.modal.installCmd") }}</span>
              </span>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-50"
                data-testid="doctor-copy-install-cmd"
                @click="copyCommand(selectedToolInstallCmd, 'install')"
              >
                <Check v-if="copiedField === 'install'" class="h-3 w-3 text-emerald-600" />
                <Copy v-else class="h-3 w-3" />
                <span>
                  {{
                    copiedField === "install"
                      ? t("overview.doctor.modal.copied")
                      : t("overview.doctor.modal.copy")
                  }}
                </span>
              </button>
            </div>
            <pre class="overflow-x-auto rounded-xl bg-slate-900 px-3.5 py-2.5 font-mono text-xs text-slate-100"><code>{{ selectedToolInstallCmd }}</code></pre>
          </div>

          <div v-if="selectedTool.docker_alt">
            <div class="mb-1.5 flex items-center justify-between">
              <span class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
                <Container class="h-3.5 w-3.5 text-sky-600" />
                <span>{{ t("overview.doctor.modal.dockerAlt") }}</span>
              </span>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-50"
                @click="copyCommand(selectedTool.docker_alt, 'docker')"
              >
                <Check v-if="copiedField === 'docker'" class="h-3 w-3 text-emerald-600" />
                <Copy v-else class="h-3 w-3" />
                <span>
                  {{
                    copiedField === "docker"
                      ? t("overview.doctor.modal.copied")
                      : t("overview.doctor.modal.copy")
                  }}
                </span>
              </button>
            </div>
            <pre class="overflow-x-auto rounded-xl bg-slate-900 px-3.5 py-2.5 font-mono text-xs text-sky-200"><code>{{ selectedTool.docker_alt }}</code></pre>
          </div>

          <div
            v-if="!selectedTool.available && !selectedTool.auto_installable"
            class="rounded-xl border border-amber-200/80 bg-amber-50/70 px-3.5 py-2.5 text-xs leading-relaxed text-amber-900"
          >
            {{ t("overview.doctor.modal.manualHint") }}
          </div>

          <div
            v-if="installError"
            class="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800"
          >
            <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
            <span>{{ installError }}</span>
          </div>

          <div v-if="installResult" class="space-y-2">
            <div
              class="flex items-center gap-2 rounded-xl border px-3.5 py-2.5 text-xs font-medium"
              :class="
                installResult.ok
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                  : 'border-rose-200 bg-rose-50 text-rose-800'
              "
            >
              <CheckCircle2 v-if="installResult.ok" class="h-4 w-4 shrink-0 text-emerald-600" />
              <AlertTriangle v-else class="h-4 w-4 shrink-0 text-rose-600" />
              <span>
                {{
                  installResult.ok
                    ? t("overview.doctor.modal.installOk")
                    : t("overview.doctor.modal.installFail", { code: installResult.returncode })
                }}
              </span>
            </div>
            <div v-if="installResult.output">
              <p class="mb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                {{ t("overview.doctor.modal.outputTitle") }}
              </p>
              <pre
                class="max-h-44 overflow-auto rounded-xl bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200"
                data-testid="doctor-install-output"
              >{{ installResult.output }}</pre>
            </div>
          </div>
        </div>

        <div class="flex items-center justify-between gap-2 border-t border-slate-100 bg-slate-50/70 px-6 py-3.5">
          <button
            type="button"
            class="btn-secondary !py-1.5 !text-xs"
            :disabled="doctorLoading || installingTool"
            @click="loadDoctor"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="doctorLoading ? 'animate-spin' : ''" />
            <span>{{ t("overview.doctor.recheck") }}</span>
          </button>
          <div class="flex items-center gap-2">
            <button
              v-if="!selectedTool.available && selectedTool.auto_installable"
              type="button"
              class="btn-primary !py-1.5 !text-xs"
              :disabled="installingTool"
              data-testid="doctor-auto-install-btn"
              @click="handleAutoInstall"
            >
              <Loader2 v-if="installingTool" class="h-3.5 w-3.5 animate-spin" />
              <Download v-else class="h-3.5 w-3.5" />
              <span>
                {{
                  installingTool
                    ? t("overview.doctor.modal.installing")
                    : t("overview.doctor.modal.autoInstall")
                }}
              </span>
            </button>
            <button
              type="button"
              class="btn-secondary !py-1.5 !text-xs"
              :disabled="installingTool"
              @click="closeToolModal"
            >
              {{ t("overview.doctor.modal.close") }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
