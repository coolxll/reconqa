<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  ShieldCheck,
  XCircle,
} from "lucide-vue-next";
import { evaluateProjectQualityGate } from "@/api/reports";
import type { GateRuleResult, QualityGateResult } from "@/api/types";
import {
  buildQualityGateRequest,
  formatGateMetricValue,
  getGateRuleState,
} from "@/utils/gateAndExport";
import { useI18n } from "@/i18n";
import "@/i18n/locales/dashboards";

const props = defineProps<{
  projectId: string;
  runId?: string | null;
  baselineRunId?: string | null;
}>();

const { locale, t } = useI18n();

const maxP0ChainFailures = ref<string>("");
const maxNewHighSecurity = ref<string>("");
const maxCoverageDropPct = ref<string>("");
const maxP95Ms = ref<string>("");

const evaluating = ref(false);
const gateError = ref<string | null>(null);
const result = ref<QualityGateResult | null>(null);

watch(
  () => [props.projectId, props.runId, props.baselineRunId] as const,
  () => {
    result.value = null;
    gateError.value = null;
  }
);

const formattedEvaluatedAt = computed(() => {
  if (!result.value?.evaluated_at) return "";
  const dt = new Date(result.value.evaluated_at);
  if (Number.isNaN(dt.getTime())) return result.value.evaluated_at;
  return dt.toLocaleString(locale.value === "zh" ? "zh-CN" : "en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
});

function ruleBadgeClass(rule: GateRuleResult): string {
  const state = getGateRuleState(rule);
  if (state === "passed") {
    return "bg-emerald-50 text-emerald-700 ring-emerald-600/20";
  }
  if (state === "failed") {
    return "bg-rose-50 text-rose-700 ring-rose-600/20";
  }
  return "bg-slate-100 text-slate-600 ring-slate-500/20";
}

function ruleBadgeLabel(rule: GateRuleResult): string {
  const state = getGateRuleState(rule);
  if (state === "passed") return t("gate.rulePassed");
  if (state === "failed") return t("gate.ruleFailed");
  return t("gate.ruleSkipped");
}

async function handleEvaluateGate(): Promise<void> {
  if (!props.projectId || evaluating.value) return;
  evaluating.value = true;
  gateError.value = null;
  try {
    const payload = buildQualityGateRequest({
      runId: props.runId,
      baselineRunId: props.baselineRunId,
      maxP0ChainFailures: maxP0ChainFailures.value,
      maxNewHighSecurity: maxNewHighSecurity.value,
      maxCoverageDropPct: maxCoverageDropPct.value,
      maxP95Ms: maxP95Ms.value,
    });
    result.value = await evaluateProjectQualityGate(props.projectId, payload);
  } catch (err) {
    gateError.value = err instanceof Error ? err.message : t("gate.errEval");
  } finally {
    evaluating.value = false;
  }
}
</script>

<template>
  <section class="card overflow-hidden" data-testid="quality-gate-card">
    <div class="card-header flex-wrap gap-3">
      <div class="flex items-center gap-2.5">
        <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
          <ShieldCheck class="h-4 w-4" />
        </div>
        <div>
          <h2 class="text-sm font-semibold text-slate-900">{{ t("gate.title") }}</h2>
          <p class="text-xs text-slate-500">{{ t("gate.subtitle") }}</p>
        </div>
      </div>

      <button
        type="button"
        data-testid="quality-gate-run-btn"
        class="btn-primary !py-1.5 !text-xs"
        :disabled="evaluating || !projectId"
        @click="handleEvaluateGate"
      >
        <Loader2 v-if="evaluating" class="h-3.5 w-3.5 animate-spin" />
        <ShieldCheck v-else class="h-3.5 w-3.5" />
        <span>{{ evaluating ? t("gate.runningBtn") : t("gate.runBtn") }}</span>
      </button>
    </div>

    <div class="space-y-4 p-5">
      <!-- Optional Threshold Overrides -->
      <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div>
          <label class="mb-1 block text-[11px] font-medium text-slate-600">
            {{ t("gate.maxP0Failures") }}
          </label>
          <input
            v-model="maxP0ChainFailures"
            data-testid="gate-input-max-p0"
            type="number"
            min="0"
            step="1"
            :placeholder="t('gate.placeholderDefault', { val: 0 })"
            class="input-field py-1.5 font-mono text-xs"
          />
        </div>
        <div>
          <label class="mb-1 block text-[11px] font-medium text-slate-600">
            {{ t("gate.maxNewHighSec") }}
          </label>
          <input
            v-model="maxNewHighSecurity"
            data-testid="gate-input-max-high-sec"
            type="number"
            min="0"
            step="1"
            :placeholder="t('gate.placeholderDefault', { val: 0 })"
            class="input-field py-1.5 font-mono text-xs"
          />
        </div>
        <div>
          <label class="mb-1 block text-[11px] font-medium text-slate-600">
            {{ t("gate.maxCovDropPct") }}
          </label>
          <input
            v-model="maxCoverageDropPct"
            data-testid="gate-input-max-cov-drop"
            type="number"
            min="0"
            step="0.5"
            :placeholder="t('gate.placeholderDefault', { val: '5.0' })"
            class="input-field py-1.5 font-mono text-xs"
          />
        </div>
        <div>
          <label class="mb-1 block text-[11px] font-medium text-slate-600">
            {{ t("gate.maxP95Ms") }}
          </label>
          <input
            v-model="maxP95Ms"
            data-testid="gate-input-max-p95"
            type="number"
            min="0"
            step="50"
            :placeholder="t('gate.placeholderOptional')"
            class="input-field py-1.5 font-mono text-xs"
          />
        </div>
      </div>

      <!-- Error Banner -->
      <div
        v-if="gateError"
        data-testid="quality-gate-error"
        class="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800"
      >
        <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
        <span>{{ gateError }}</span>
      </div>

      <!-- Result Banner + Per-Rule Breakdown -->
      <div v-if="result" class="space-y-3" data-testid="quality-gate-result">
        <div
          class="flex flex-wrap items-center justify-between gap-2 rounded-xl border px-4 py-3"
          :class="
            result.passed
              ? 'border-emerald-200 bg-emerald-50/70 text-emerald-950'
              : 'border-rose-200 bg-rose-50/70 text-rose-950'
          "
        >
          <div class="flex flex-wrap items-center gap-2.5">
            <span
              data-testid="quality-gate-status-badge"
              class="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-bold ring-1 ring-inset"
              :class="
                result.passed
                  ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
                  : 'bg-rose-50 text-rose-700 ring-rose-600/20'
              "
            >
              <CheckCircle2 v-if="result.passed" class="h-3.5 w-3.5 text-emerald-600" />
              <XCircle v-else class="h-3.5 w-3.5 text-rose-600" />
              <span>{{ result.passed ? t("gate.statusPassed") : t("gate.statusFailed") }}</span>
            </span>
            <span class="text-xs font-medium">{{ result.summary }}</span>
          </div>

          <span v-if="formattedEvaluatedAt" class="font-mono text-[11px] text-slate-500">
            {{ t("gate.evaluatedAt", { time: formattedEvaluatedAt }) }}
          </span>
        </div>

        <div class="overflow-x-auto rounded-xl border border-slate-200">
          <table
            data-testid="quality-gate-rules-table"
            class="min-w-full divide-y divide-slate-200 text-left text-xs"
          >
            <thead class="table-head">
              <tr>
                <th class="px-4 py-2.5">{{ t("gate.colRule") }}</th>
                <th class="px-4 py-2.5">{{ t("gate.colStatus") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("gate.colActual") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("gate.colThreshold") }}</th>
                <th class="px-4 py-2.5">{{ t("gate.colMessage") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 bg-white">
              <tr v-for="rule in result.rules" :key="rule.rule" class="hover:bg-slate-50/70">
                <td class="px-4 py-2.5 font-medium text-slate-800">
                  <div>{{ rule.label }}</div>
                  <div class="font-mono text-[10px] text-slate-400">{{ rule.rule }}</div>
                </td>
                <td class="whitespace-nowrap px-4 py-2.5">
                  <span
                    class="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold ring-1 ring-inset"
                    :class="ruleBadgeClass(rule)"
                  >
                    {{ ruleBadgeLabel(rule) }}
                  </span>
                </td>
                <td class="whitespace-nowrap px-4 py-2.5 text-right font-mono text-slate-800">
                  {{ formatGateMetricValue(rule, rule.actual) }}
                </td>
                <td class="whitespace-nowrap px-4 py-2.5 text-right font-mono text-slate-500">
                  {{ formatGateMetricValue(rule, rule.threshold) }}
                </td>
                <td class="px-4 py-2.5 text-slate-600">{{ rule.message }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <p v-else-if="!gateError" class="text-xs text-slate-500">
        {{ t("gate.emptyHint") }}
      </p>
    </div>
  </section>
</template>
