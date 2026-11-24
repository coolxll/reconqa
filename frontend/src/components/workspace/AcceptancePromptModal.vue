<script setup lang="ts">
import { ref, watch } from "vue";
import {
  AlertTriangle,
  Bot,
  Check,
  Compass,
  Copy,
  Globe,
  Loader2,
  RefreshCw,
  Sparkles,
  X,
} from "lucide-vue-next";
import { getAcceptancePrompt, listTargets } from "@/api/projects";
import type {
  AcceptancePromptAgent,
  AcceptancePromptMode,
  AcceptancePromptResponse,
  Target,
} from "@/api/types";
import { useI18n } from "@/i18n";
import { copyToClipboard } from "@/utils/clipboard";

const props = withDefaults(
  defineProps<{
    open: boolean;
    projectId: string;
    initialMode?: AcceptancePromptMode;
  }>(),
  {
    initialMode: "full",
  }
);

const emit = defineEmits<{
  close: [];
}>();

const { locale, t } = useI18n();

const AGENTS: AcceptancePromptAgent[] = ["generic", "antigravity", "claude", "codex", "openclaw"];

const promptMode = ref<AcceptancePromptMode>(props.initialMode || "full");
const promptLang = ref<"zh" | "en">(locale.value === "en" ? "en" : "zh");
const agentPreset = ref<AcceptancePromptAgent>("antigravity");
const loading = ref(false);
const errorMsg = ref<string | null>(null);
const data = ref<AcceptancePromptResponse | null>(null);
const copied = ref(false);

// Portal targets cache (for har_explore scope pills)
const targets = ref<Target[]>([]);
const targetsLoadedForProject = ref("");
const selectedTargetIds = ref<Set<string>>(new Set());

let fetchSeq = 0;

function targetDisplayLabel(tItem: Target): string {
  const label =
    tItem.scope_rules && typeof tItem.scope_rules.portal_label === "string"
      ? tItem.scope_rules.portal_label.trim()
      : "";
  if (label) return label;
  try {
    return new URL(tItem.base_url).hostname;
  } catch {
    return tItem.root_domain || tItem.base_url;
  }
}

async function ensureTargetsLoaded(): Promise<void> {
  const pid = props.projectId;
  if (!pid || targetsLoadedForProject.value === pid) return;
  try {
    const list = await listTargets(pid);
    if (props.projectId !== pid) return;
    targets.value = list;
    targetsLoadedForProject.value = pid;
    selectedTargetIds.value = new Set(list.map((item) => item.id));
  } catch {
    targets.value = [];
  }
}

function toggleTarget(id: string): void {
  const next = new Set(selectedTargetIds.value);
  if (next.has(id)) {
    next.delete(id);
  } else {
    next.add(id);
  }
  selectedTargetIds.value = next;
  void fetchPrompt();
}

function selectAllTargets(): void {
  selectedTargetIds.value = new Set(targets.value.map((item) => item.id));
  void fetchPrompt();
}

async function fetchPrompt(): Promise<void> {
  const pid = props.projectId;
  if (!props.open || !pid) return;
  const reqId = ++fetchSeq;
  loading.value = true;
  errorMsg.value = null;
  try {
    const targetIdsParam =
      promptMode.value === "har_explore" && selectedTargetIds.value.size > 0
        ? Array.from(selectedTargetIds.value)
        : undefined;
    const res = await getAcceptancePrompt(pid, {
      lang: promptLang.value,
      agent: agentPreset.value,
      mode: promptMode.value,
      targetIds: targetIdsParam,
    });
    if (reqId !== fetchSeq || props.projectId !== pid) return;
    data.value = res;
  } catch (err) {
    if (reqId !== fetchSeq) return;
    errorMsg.value = err instanceof Error ? err.message : t("acceptPrompt.error");
  } finally {
    if (reqId === fetchSeq) {
      loading.value = false;
    }
  }
}

async function copyPrompt(): Promise<void> {
  const text = data.value?.prompt_markdown;
  if (!text) return;
  const ok = await copyToClipboard(text);
  if (!ok) return;
  copied.value = true;
  window.setTimeout(() => {
    copied.value = false;
  }, 2000);
}

watch(
  () => props.open,
  async (isOpen) => {
    if (isOpen) {
      promptMode.value = props.initialMode || "full";
      promptLang.value = locale.value === "en" ? "en" : "zh";
      copied.value = false;
      if (promptMode.value === "har_explore") {
        await ensureTargetsLoaded();
      }
      void fetchPrompt();
    } else {
      targetsLoadedForProject.value = "";
    }
  }
);

watch(promptMode, async (mode) => {
  if (!props.open) return;
  if (mode === "har_explore") {
    await ensureTargetsLoaded();
  }
  void fetchPrompt();
});

watch([promptLang, agentPreset], () => {
  if (props.open) {
    void fetchPrompt();
  }
});

watch(
  () => props.projectId,
  async () => {
    targetsLoadedForProject.value = "";
    targets.value = [];
    selectedTargetIds.value = new Set();
    if (props.open) {
      if (promptMode.value === "har_explore") {
        await ensureTargetsLoaded();
      }
      void fetchPrompt();
    }
  }
);
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4 backdrop-blur-sm"
      data-testid="acceptance-prompt-modal"
      @click.self="emit('close')"
    >
      <div class="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
        <!-- Header -->
        <div class="flex items-start justify-between gap-4 border-b border-slate-200 bg-gradient-to-r from-slate-900 to-indigo-950 px-6 py-4 text-white">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-500/25 ring-1 ring-inset ring-indigo-400/40">
                <Bot class="h-4 w-4 text-indigo-200" />
              </span>
              <h2 class="text-base font-bold tracking-tight">{{ t("acceptPrompt.title") }}</h2>
            </div>
            <p class="mt-1 text-xs leading-relaxed text-slate-300">
              {{ t("acceptPrompt.subtitle") }}
            </p>
          </div>
          <button
            type="button"
            class="rounded-lg p-1.5 text-slate-400 transition hover:bg-white/10 hover:text-white"
            @click="emit('close')"
          >
            <X class="h-5 w-5" />
          </button>
        </div>

        <!-- Controls & Live Context Badges -->
        <div class="space-y-3 border-b border-slate-200 bg-slate-50/80 px-6 py-3.5">
          <!-- 2-Tab Mode Switcher -->
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-xs font-semibold text-slate-600">
                {{ t("acceptPrompt.modeLabel") }}:
              </span>
              <div class="inline-flex rounded-lg border border-slate-200 bg-slate-200/70 p-0.5 text-xs font-semibold">
                <button
                  type="button"
                  data-testid="prompt-mode-har_explore"
                  class="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 transition"
                  :class="
                    promptMode === 'har_explore'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="promptMode = 'har_explore'"
                >
                  <Compass class="h-3.5 w-3.5" />
                  <span>{{ t("acceptPrompt.mode.har_explore") }}</span>
                </button>
                <button
                  type="button"
                  data-testid="prompt-mode-full"
                  class="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 transition"
                  :class="
                    promptMode === 'full'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="promptMode = 'full'"
                >
                  <Sparkles class="h-3.5 w-3.5" />
                  <span>{{ t("acceptPrompt.mode.full") }}</span>
                </button>
              </div>
            </div>

            <!-- Language Toggle + Refresh -->
            <div class="flex items-center gap-2">
              <div class="inline-flex rounded-lg border border-slate-200 bg-white p-0.5 text-xs font-medium">
                <button
                  type="button"
                  data-testid="prompt-lang-zh"
                  class="rounded-md px-2.5 py-1 transition"
                  :class="promptLang === 'zh' ? 'bg-indigo-600 text-white' : 'text-slate-600'"
                  @click="promptLang = 'zh'"
                >
                  中文
                </button>
                <button
                  type="button"
                  data-testid="prompt-lang-en"
                  class="rounded-md px-2.5 py-1 transition"
                  :class="promptLang === 'en' ? 'bg-indigo-600 text-white' : 'text-slate-600'"
                  @click="promptLang = 'en'"
                >
                  EN
                </button>
              </div>
              <button
                type="button"
                class="btn-secondary !px-2.5 !py-1.5 !text-xs"
                :disabled="loading"
                @click="fetchPrompt"
              >
                <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
              </button>
            </div>
          </div>

          <!-- Agent Preset Pills -->
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-xs font-semibold text-slate-600">
              {{ t("acceptPrompt.agentLabel") }}:
            </span>
            <div class="inline-flex flex-wrap items-center gap-1 rounded-lg bg-slate-200/70 p-0.5">
              <button
                v-for="ag in AGENTS"
                :key="ag"
                type="button"
                :data-testid="`prompt-agent-${ag}`"
                class="rounded-md px-2.5 py-1 text-xs font-medium transition"
                :class="
                  agentPreset === ag
                    ? 'bg-white text-indigo-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900'
                "
                @click="agentPreset = ag"
              >
                {{ t(`acceptPrompt.agent.${ag}`) }}
              </button>
            </div>
          </div>

          <!-- Portal Target Scope Pills (when har_explore mode is active) -->
          <div
            v-if="promptMode === 'har_explore' && targets.length > 0"
            data-testid="prompt-target-selector"
            class="flex flex-wrap items-center gap-1.5 rounded-lg border border-indigo-100 bg-indigo-50/50 px-3 py-2 text-xs"
          >
            <span class="inline-flex items-center gap-1 font-semibold text-indigo-800">
              <Globe class="h-3.5 w-3.5" />
              <span>{{ t("acceptPrompt.portalsScopeLabel") }}:</span>
            </span>

            <button
              v-for="tItem in targets"
              :key="tItem.id"
              type="button"
              data-testid="prompt-target-pill"
              class="inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition"
              :class="
                selectedTargetIds.has(tItem.id)
                  ? 'border-indigo-600 bg-indigo-600 text-white'
                  : 'border-slate-300 bg-white text-slate-600 hover:border-indigo-300 hover:text-indigo-700'
              "
              @click="toggleTarget(tItem.id)"
            >
              <Check v-if="selectedTargetIds.has(tItem.id)" class="h-3 w-3 shrink-0" />
              <span>{{ targetDisplayLabel(tItem) }}</span>
            </button>

            <button
              v-if="selectedTargetIds.size < targets.length"
              type="button"
              class="ml-1 text-[11px] font-semibold text-indigo-600 hover:underline"
              @click="selectAllTargets"
            >
              {{ t("acceptPrompt.portalsSelectAll") }}
            </button>
            <span
              v-if="selectedTargetIds.size === 0"
              class="ml-1 text-[11px] text-slate-500"
            >
              ({{ t("acceptPrompt.portalsAllHint") }})
            </span>
          </div>

          <!-- Context Summary Badges -->
          <div
            v-if="data?.context_summary"
            data-testid="acceptance-prompt-context"
            class="flex flex-wrap items-center gap-2 text-[11px]"
          >
            <span class="rounded-full bg-white px-2.5 py-0.5 font-medium text-slate-700 ring-1 ring-slate-200">
              {{ t("acceptPrompt.ctxTargets") }}:
              <strong class="font-mono text-slate-900">{{ data.context_summary.targets_count }}</strong>
            </span>
            <span class="rounded-full bg-white px-2.5 py-0.5 font-medium text-slate-700 ring-1 ring-slate-200">
              {{ t("acceptPrompt.ctxCreds") }}:
              <strong class="font-mono text-slate-900">{{ data.context_summary.credentials_count }}</strong>
            </span>
            <span class="rounded-full bg-white px-2.5 py-0.5 font-medium text-slate-700 ring-1 ring-slate-200">
              {{ t("acceptPrompt.ctxKb") }}:
              <strong class="font-mono text-slate-900">{{ data.context_summary.kb_docs_count }}</strong>
            </span>
            <span class="rounded-full bg-indigo-50 px-2.5 py-0.5 font-medium text-indigo-800 ring-1 ring-indigo-200">
              {{ t("acceptPrompt.ctxCoverage") }}:
              <strong class="font-mono">{{ data.context_summary.coverage_pct.toFixed(1) }}%</strong>
            </span>
            <span class="rounded-full bg-emerald-50 px-2.5 py-0.5 font-medium text-emerald-800 ring-1 ring-emerald-200">
              {{ t("acceptPrompt.ctxEffective") }}:
              <strong class="font-mono">{{ data.context_summary.p0_p1_effective_pct.toFixed(1) }}%</strong>
            </span>
            <span
              class="rounded-full px-2.5 py-0.5 font-medium ring-1"
              :class="
                data.context_summary.scenarios_needing_attention > 0
                  ? 'bg-amber-50 text-amber-800 ring-amber-200'
                  : 'bg-slate-100 text-slate-600 ring-slate-200'
              "
            >
              {{ t("acceptPrompt.ctxAttention") }}:
              <strong class="font-mono">{{ data.context_summary.scenarios_needing_attention }}</strong>
            </span>
          </div>
        </div>

        <!-- Prompt Markdown Body -->
        <div class="flex-1 overflow-y-auto p-6">
          <div
            v-if="loading"
            class="flex items-center justify-center gap-2 py-16 text-sm text-slate-500"
          >
            <Loader2 class="h-4 w-4 animate-spin text-indigo-600" />
            <span>{{ t("acceptPrompt.loading") }}</span>
          </div>

          <div
            v-else-if="errorMsg"
            class="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
          >
            <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
            <span>{{ errorMsg }}</span>
          </div>

          <pre
            v-else-if="data"
            data-testid="acceptance-prompt-markdown"
            class="overflow-x-auto whitespace-pre-wrap rounded-xl border border-slate-800 bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-100"
          >{{ data.prompt_markdown }}</pre>
        </div>

        <!-- Footer -->
        <div class="flex items-center justify-between border-t border-slate-200 bg-slate-50 px-6 py-3.5">
          <span class="inline-flex items-center gap-1.5 text-xs text-slate-500">
            <Sparkles class="h-3.5 w-3.5 text-indigo-500" />
            <span>{{ data?.project_name || "" }}</span>
          </span>
          <div class="flex items-center gap-2.5">
            <button type="button" class="btn-secondary" @click="emit('close')">
              {{ t("common.cancel") }}
            </button>
            <button
              type="button"
              data-testid="acceptance-prompt-copy-btn"
              class="btn-primary px-4"
              :disabled="!data?.prompt_markdown || loading"
              @click="copyPrompt"
            >
              <Check v-if="copied" class="h-4 w-4 text-emerald-200" />
              <Copy v-else class="h-4 w-4" />
              <span>{{ copied ? t("acceptPrompt.copiedBtn") : t("acceptPrompt.copyBtn") }}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
