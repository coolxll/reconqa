<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Coffee,
  Gauge,
  KeyRound,
  Loader2,
  ShieldAlert,
  Sparkles,
  Terminal,
  Wrench,
} from "lucide-vue-next";
import { executeRunExternalTool, listAuthProfiles } from "@/api/projects";
import type {
  AuthProfile,
  ExecuteExternalToolResponse,
  ExternalToolName,
} from "@/api/types";
import { useI18n } from "@/i18n";

const props = defineProps<{
  projectId: string;
  runId: string;
}>();

const emit = defineEmits<{
  executed: [];
}>();

const { t } = useI18n();

const TOOLS: Array<{ id: ExternalToolName; icon: typeof Coffee; tone: string }> = [
  { id: "java_test", icon: Coffee, tone: "border-indigo-200 bg-indigo-50/70 text-indigo-800 hover:bg-indigo-100" },
  { id: "schemathesis", icon: Sparkles, tone: "border-teal-200 bg-teal-50/70 text-teal-800 hover:bg-teal-100" },
  { id: "nuclei", icon: ShieldAlert, tone: "border-rose-200 bg-rose-50/70 text-rose-800 hover:bg-rose-100" },
  { id: "k6", icon: Gauge, tone: "border-emerald-200 bg-emerald-50/70 text-emerald-800 hover:bg-emerald-100" },
];

const authProfiles = ref<AuthProfile[]>([]);
const selectedCredentialId = ref("");
const syncScenarios = ref(true);
const runningTool = ref<ExternalToolName | null>(null);
const result = ref<ExecuteExternalToolResponse | null>(null);
const execError = ref<string | null>(null);
const showLogs = ref(false);

let profileLoadSeq = 0;

async function loadProfiles(): Promise<void> {
  const targetPid = props.projectId;
  const reqId = ++profileLoadSeq;
  if (!targetPid) {
    authProfiles.value = [];
    selectedCredentialId.value = "";
    return;
  }
  try {
    const list = await listAuthProfiles(targetPid);
    if (reqId !== profileLoadSeq || props.projectId !== targetPid) return;
    authProfiles.value = list;
    if (list.length > 0 && !selectedCredentialId.value) {
      selectedCredentialId.value = list[0].id;
    }
  } catch {
    if (reqId !== profileLoadSeq || props.projectId !== targetPid) return;
    authProfiles.value = [];
  }
}

async function runTool(tool: ExternalToolName): Promise<void> {
  if (!props.projectId || !props.runId || runningTool.value) return;
  const targetPid = props.projectId;
  const targetRunId = props.runId;
  runningTool.value = tool;
  execError.value = null;
  result.value = null;
  showLogs.value = false;
  try {
    const res = await executeRunExternalTool(targetPid, targetRunId, {
      tool,
      credential_id: selectedCredentialId.value || null,
      sync_scenarios: syncScenarios.value,
      timeout_sec: 120,
    });
    if (props.projectId !== targetPid || props.runId !== targetRunId) return;
    result.value = res;
    if (res.executed) {
      emit("executed");
    }
  } catch (err) {
    if (props.projectId !== targetPid || props.runId !== targetRunId) return;
    execError.value = err instanceof Error ? err.message : t("extTool.errExec");
  } finally {
    runningTool.value = null;
  }
}

function statusPillClass(status: string): string {
  if (status === "passed" || status === "completed" || status === "ok") {
    return "bg-emerald-50 text-emerald-700 ring-emerald-600/25";
  }
  if (status === "tool_missing") {
    return "bg-amber-50 text-amber-800 ring-amber-600/25";
  }
  return "bg-rose-50 text-rose-700 ring-rose-600/25";
}

function formatSummaryEntries(summary: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(summary || {})
    .filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .slice(0, 8)
    .map(([k, v]) => [k, String(v)]);
}

watch(() => props.projectId, () => {
  selectedCredentialId.value = "";
  authProfiles.value = [];
  result.value = null;
  execError.value = null;
  showLogs.value = false;
  void loadProfiles();
});

watch(() => props.runId, () => {
  result.value = null;
  execError.value = null;
  showLogs.value = false;
});

onMounted(() => {
  void loadProfiles();
});
</script>

<template>
  <section
    class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm"
    data-testid="external-tool-runner-card"
  >
    <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50/70 px-5 py-3.5">
      <div>
        <div class="flex items-center gap-2">
          <Wrench class="h-4 w-4 text-indigo-600" />
          <h3 class="text-sm font-semibold text-slate-900">{{ t("extTool.title") }}</h3>
        </div>
        <p class="mt-0.5 text-xs text-slate-500">{{ t("extTool.subtitle") }}</p>
      </div>

      <div class="flex flex-wrap items-center gap-3">
        <!-- Credential Selector -->
        <div class="flex items-center gap-1.5">
          <KeyRound class="h-3.5 w-3.5 text-slate-400" />
          <select
            v-model="selectedCredentialId"
            data-testid="ext-tool-credential-select"
            class="input-field w-44 !py-1 text-xs"
            :aria-label="t('extTool.credentialLabel')"
            :disabled="Boolean(runningTool)"
          >
            <option value="">{{ t("extTool.credentialNone") }}</option>
            <option v-for="cred in authProfiles" :key="cred.id" :value="cred.id">
              {{ cred.name }} ({{ cred.role_tag }})
            </option>
          </select>
        </div>

        <!-- Sync-Back Checkbox -->
        <label class="inline-flex cursor-pointer items-center gap-1.5 text-xs text-slate-700">
          <input
            v-model="syncScenarios"
            type="checkbox"
            class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
            :disabled="Boolean(runningTool)"
          />
          <span>{{ t("extTool.syncToggle") }}</span>
        </label>
      </div>
    </div>

    <div class="space-y-4 p-5">
      <!-- 4 Tool Trigger Buttons -->
      <div class="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
        <button
          v-for="tool in TOOLS"
          :key="tool.id"
          type="button"
          :data-testid="`ext-tool-btn-${tool.id}`"
          :disabled="Boolean(runningTool)"
          class="inline-flex items-center justify-center gap-2 rounded-xl border px-3 py-2.5 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-50"
          :class="tool.tone"
          @click="runTool(tool.id)"
        >
          <Loader2 v-if="runningTool === tool.id" class="h-4 w-4 animate-spin" />
          <component :is="tool.icon" v-else class="h-4 w-4" />
          <span>
            {{
              runningTool === tool.id
                ? t("extTool.running", { tool: tool.id })
                : t(`extTool.btn.${tool.id}`)
            }}
          </span>
        </button>
      </div>

      <!-- Error Notice -->
      <div
        v-if="execError"
        class="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800"
      >
        <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
        <span>{{ execError }}</span>
      </div>

      <!-- Execution Result Panel -->
      <div
        v-if="result"
        data-testid="ext-tool-result"
        class="space-y-3 rounded-xl border border-slate-200 bg-slate-50/70 p-4"
      >
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-mono text-xs font-bold text-slate-900">{{ result.tool }}</span>
            <span
              class="inline-flex rounded-full px-2.5 py-0.5 text-[11px] font-semibold ring-1 ring-inset"
              :class="statusPillClass(result.status)"
            >
              {{ result.status === "tool_missing" ? t("extTool.statusMissing") : result.status }}
            </span>
            <span class="inline-flex items-center gap-1 font-mono text-[11px] text-slate-500">
              <Clock class="h-3 w-3" />
              {{ Math.round(result.duration_ms) }} ms
            </span>
          </div>

          <router-link
            v-if="result.status === 'tool_missing'"
            to="/system/doctor"
            class="text-xs font-semibold text-indigo-600 hover:text-indigo-800 hover:underline"
          >
            {{ t("extTool.openDoctor") }}
          </router-link>
        </div>

        <!-- Tool error / missing message -->
        <div
          v-if="result.error"
          class="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900"
        >
          {{ result.error }}
        </div>

        <!-- Scenario Sync-Back Badge -->
        <div
          v-if="
            result.synced_scenarios > 0 ||
            result.synced_assertions > 0 ||
            result.synced_negative_cases > 0
          "
          data-testid="ext-tool-synced-badge"
          class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3.5 py-2 text-xs text-emerald-900"
        >
          <div class="flex items-center gap-2">
            <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-600" />
            <span class="font-medium">
              {{
                t("extTool.syncedBadge", {
                  scenarios: result.synced_scenarios,
                  assertions: result.synced_assertions,
                  negatives: result.synced_negative_cases,
                })
              }}
            </span>
          </div>
          <router-link
            :to="`/p/${encodeURIComponent(projectId)}/scenarios`"
            class="font-semibold text-emerald-800 hover:underline"
          >
            {{ t("extTool.viewScenarios") }}
          </router-link>
        </div>

        <!-- Compact Summary Key-Value Strip -->
        <div
          v-if="formatSummaryEntries(result.summary).length > 0"
          class="flex flex-wrap gap-2"
        >
          <span
            v-for="[k, v] in formatSummaryEntries(result.summary)"
            :key="k"
            class="rounded-md border border-slate-200 bg-white px-2.5 py-1 font-mono text-[11px] text-slate-700"
          >
            <span class="text-slate-400">{{ k }}:</span>
            <strong class="ml-1 text-slate-900">{{ v }}</strong>
          </span>
        </div>

        <!-- Collapsible stdout_tail -->
        <div v-if="result.stdout_tail">
          <button
            type="button"
            class="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800"
            @click="showLogs = !showLogs"
          >
            <ChevronDown v-if="showLogs" class="h-3.5 w-3.5" />
            <ChevronRight v-else class="h-3.5 w-3.5" />
            <Terminal class="h-3.5 w-3.5" />
            <span>{{ showLogs ? t("extTool.hideLogs") : t("extTool.toggleLogs") }}</span>
          </button>
          <pre
            v-if="showLogs"
            class="mt-2 max-h-60 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200"
          >{{ result.stdout_tail }}</pre>
        </div>
      </div>
    </div>
  </section>
</template>
