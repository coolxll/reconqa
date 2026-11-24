<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import mermaid from 'mermaid'
import {
  AlertTriangle,
  Check,
  Code2,
  Copy,
  Eye,
  GitBranch,
  Layers,
  ShieldAlert,
} from 'lucide-vue-next'
import type { CorrelationReport, CoverageDiffReport } from '@/api/types'
import { getRunCorrelation, getRunCoverage } from '@/api/runs'
import StatusBadge from '@/components/layout/StatusBadge.vue'
import EmptyState from '@/components/layout/EmptyState.vue'
import '@/i18n/locales/runs'
import { useI18n } from '@/i18n'

const { t } = useI18n()

const props = defineProps<{
  runId: string
}>()

const loading = ref(true)
const correlation = ref<CorrelationReport | null>(null)
const coverage = ref<CoverageDiffReport | null>(null)
const renderedSvg = ref('')
const mermaidError = ref('')
const showRawMermaid = ref(false)
const copiedMermaid = ref(false)

let mermaidReady = false
let renderCounter = 0

function initMermaidOnce() {
  if (mermaidReady) return
  mermaid.initialize({
    startOnLoad: false,
    theme: 'neutral',
    securityLevel: 'strict',
    flowchart: {
      useMaxWidth: true,
      htmlLabels: true,
      curve: 'basis',
    },
  })
  mermaidReady = true
}

async function renderDiagram(definition: string) {
  renderedSvg.value = ''
  mermaidError.value = ''
  const trimmed = definition.trim()
  if (!trimmed) return

  initMermaidOnce()
  renderCounter += 1
  const safeId = `reconqa-mermaid-${renderCounter}-${Date.now()}`
  try {
    const { svg } = await mermaid.render(safeId, trimmed)
    renderedSvg.value = svg
  } catch (err) {
    mermaidError.value =
      err instanceof Error ? err.message : 'Unable to render Mermaid SVG diagram.'
    showRawMermaid.value = true
  }
}

async function loadData() {
  loading.value = true
  correlation.value = null
  coverage.value = null
  renderedSvg.value = ''
  mermaidError.value = ''

  const [corrRes, covRes] = await Promise.allSettled([
    getRunCorrelation(props.runId),
    getRunCoverage(props.runId),
  ])

  if (corrRes.status === 'fulfilled') {
    correlation.value = corrRes.value
    if (corrRes.value?.mermaid) {
      await renderDiagram(corrRes.value.mermaid)
    }
  }
  if (covRes.status === 'fulfilled') {
    coverage.value = covRes.value
  }
  loading.value = false
}

async function copyMermaidCode() {
  const code = correlation.value?.mermaid ?? ''
  if (!code) return
  try {
    await navigator.clipboard.writeText(code)
    copiedMermaid.value = true
    window.setTimeout(() => {
      copiedMermaid.value = false
    }, 1800)
  } catch {
    // ignore clipboard errors
  }
}

const coveragePct = computed(() => {
  const pct = coverage.value?.coverage_pct ?? 0
  return Math.max(0, Math.min(100, Number(pct)))
})

const ringCircumference = 2 * Math.PI * 42
const ringDashOffset = computed(
  () => ringCircumference * (1 - coveragePct.value / 100),
)

function confidenceColor(score: number): string {
  if (score >= 0.85) return 'bg-emerald-500'
  if (score >= 0.65) return 'bg-indigo-500'
  return 'bg-amber-500'
}

watch(() => props.runId, loadData)
onMounted(loadData)
</script>

<template>
  <div class="space-y-6">
    <div
      v-if="loading"
      class="flex items-center justify-center rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500"
    >
      {{ t('corr.loading') }}
    </div>

    <EmptyState
      v-else-if="!correlation && !coverage"
      :title="t('corr.emptyTitle')"
      :description="t('corr.emptyDesc')"
    />

    <template v-else>
      <!-- Mermaid Data-Flow Correlation Graph -->
      <section v-if="correlation" class="rounded-xl border border-slate-200 bg-white shadow-sm">
        <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div class="flex items-center gap-2.5">
            <GitBranch class="h-5 w-5 text-indigo-600" />
            <div>
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t('corr.graphTitle') }}
              </h3>
              <p class="text-xs text-slate-500">
                {{ t('corr.graphSubtitle', { vars: correlation.variables.length, chains: correlation.chains.length }) }}
              </p>
            </div>
          </div>

          <div v-if="correlation.mermaid" class="flex items-center gap-2">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
              @click="showRawMermaid = !showRawMermaid"
            >
              <Eye v-if="showRawMermaid" class="h-3.5 w-3.5" />
              <Code2 v-else class="h-3.5 w-3.5" />
              <span>{{ showRawMermaid ? t('corr.viewDiagram') : t('corr.rawMermaid') }}</span>
            </button>

            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
              @click="copyMermaidCode"
            >
              <Check v-if="copiedMermaid" class="h-3.5 w-3.5 text-emerald-600" />
              <Copy v-else class="h-3.5 w-3.5" />
              <span>{{ copiedMermaid ? t('corr.copied') : t('corr.copyMermaid') }}</span>
            </button>
          </div>
        </div>

        <div class="p-5">
          <div
            v-if="mermaidError"
            class="mb-3 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
          >
            <AlertTriangle class="h-4 w-4 shrink-0" />
            <span>{{ t('corr.mermaidFallback') }} {{ mermaidError }}</span>
          </div>

          <div
            v-if="!correlation.mermaid"
            class="rounded-lg border border-dashed border-slate-200 bg-slate-50 py-8 text-center text-xs text-slate-500"
          >
            {{ t('corr.noGraphEdges') }}
          </div>

          <pre
            v-else-if="showRawMermaid || !renderedSvg"
            class="max-h-96 overflow-auto rounded-lg bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-100"
          >{{ correlation.mermaid }}</pre>

          <div
            v-else
            class="mermaid-container overflow-x-auto rounded-lg border border-slate-100 bg-slate-50/50 p-4 [&>svg]:mx-auto [&>svg]:h-auto [&>svg]:max-w-full"
            v-html="renderedSvg"
          />

          <!-- Chains Summary Strip -->
          <div
            v-if="correlation.chains.length > 0"
            class="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2"
          >
            <div
              v-for="chain in correlation.chains"
              :key="chain.name"
              class="flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50/80 px-3.5 py-2.5 text-xs"
            >
              <div>
                <div class="font-semibold text-slate-800">{{ chain.name }}</div>
                <div class="mt-0.5 text-slate-500">
                  {{ t('corr.chainMeta', { scenario: chain.scenario, steps: chain.steps.length }) }}
                </div>
              </div>
              <span
                v-if="chain.cleanup_step"
                class="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700"
              >
                {{ t('corr.cleanupBadge', { method: chain.cleanup_step.method, path: chain.cleanup_step.path }) }}
              </span>
            </div>
          </div>
        </div>
      </section>

      <!-- Correlated Variables Table & Filtered Static Constants -->
      <section v-if="correlation" class="rounded-xl border border-slate-200 bg-white shadow-sm">
        <div class="border-b border-slate-200 px-5 py-4">
          <h3 class="text-sm font-semibold text-slate-900">
            {{ t('corr.varsTitle', { count: correlation.variables.length }) }}
          </h3>
          <p class="mt-0.5 text-xs text-slate-500">
            {{ t('corr.varsSubtitle') }}
          </p>
        </div>

        <div class="overflow-x-auto">
          <table class="min-w-full divide-y divide-slate-200 text-left text-xs">
            <thead class="bg-slate-50 font-semibold tracking-wider text-slate-500 uppercase">
              <tr>
                <th class="px-5 py-3">{{ t('corr.col.variable') }}</th>
                <th class="px-4 py-3">{{ t('corr.col.producer') }}</th>
                <th class="px-4 py-3">{{ t('corr.col.consumers') }}</th>
                <th class="w-40 px-5 py-3">{{ t('corr.col.confidence') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr
                v-for="variable in correlation.variables"
                :key="variable.var_name"
                class="hover:bg-slate-50/70"
              >
                <td class="px-5 py-3 align-top">
                  <span class="inline-flex rounded-md border border-indigo-200 bg-indigo-50 px-2 py-0.5 font-mono font-semibold text-indigo-700">
                    &#123;&#123;{{ variable.var_name }}&#125;&#125;
                  </span>
                </td>
                <td class="px-4 py-3 align-top">
                  <div class="flex items-center gap-1.5 font-mono text-slate-800">
                    <span class="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold">
                      #{{ variable.producer.step }}
                    </span>
                    <span class="font-semibold text-emerald-700">{{ variable.producer.method }}</span>
                    <span>{{ variable.producer.path }}</span>
                  </div>
                  <div v-if="variable.producer.jsonpath" class="mt-1 font-mono text-[11px] text-indigo-600">
                    {{ variable.producer.jsonpath }}
                  </div>
                </td>
                <td class="px-4 py-3 align-top">
                  <ul class="space-y-1">
                    <li
                      v-for="(consumer, idx) in variable.consumers"
                      :key="`${variable.var_name}-${idx}`"
                      class="flex flex-wrap items-center gap-1.5 font-mono text-slate-700"
                    >
                      <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-600">
                        #{{ consumer.step }}
                      </span>
                      <span class="font-semibold text-sky-700">{{ consumer.method }}</span>
                      <span>{{ consumer.path }}</span>
                      <span
                        v-if="consumer.location || consumer.param"
                        class="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800"
                      >
                        {{ consumer.location }}:{{ consumer.param }}
                      </span>
                    </li>
                  </ul>
                </td>
                <td class="px-5 py-3 align-top">
                  <div class="flex items-center gap-2">
                    <div class="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                      <div
                        class="h-full rounded-full transition-all"
                        :class="confidenceColor(variable.confidence)"
                        :style="{ width: `${Math.round(variable.confidence * 100)}%` }"
                      />
                    </div>
                    <span class="w-9 text-right font-mono font-semibold text-slate-700">
                      {{ Math.round(variable.confidence * 100) }}%
                    </span>
                  </div>
                </td>
              </tr>

              <tr v-if="correlation.variables.length === 0">
                <td colspan="4" class="px-5 py-8 text-center text-slate-500">
                  {{ t('corr.noVars') }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Filtered Static Constants -->
        <div class="border-t border-slate-200 bg-slate-50/70 px-5 py-3.5">
          <div class="mb-2 text-xs font-semibold text-slate-600">
            {{ t('corr.filteredTitle', { count: correlation.filtered_constants.length }) }}
            <span class="font-normal text-slate-400">{{ t('corr.filteredHint') }}</span>
          </div>
          <div v-if="correlation.filtered_constants.length === 0" class="text-xs text-slate-400">
            {{ t('corr.noneFiltered') }}
          </div>
          <div v-else class="flex flex-wrap gap-1.5">
            <span
              v-for="item in correlation.filtered_constants"
              :key="item"
              class="rounded-full border border-slate-200 bg-white px-2.5 py-0.5 font-mono text-[11px] text-slate-600"
            >
              {{ item }}
            </span>
          </div>
        </div>
      </section>

      <!-- Coverage Diff Breakdown -->
      <section v-if="coverage" class="space-y-4">
        <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <!-- Overall Coverage Ring -->
          <div class="flex items-center gap-5 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div class="relative h-24 w-24 shrink-0">
              <svg viewBox="0 0 100 100" class="h-full w-full -rotate-90">
                <circle
                  cx="50"
                  cy="50"
                  r="42"
                  fill="none"
                  stroke-width="10"
                  class="stroke-slate-100"
                />
                <circle
                  cx="50"
                  cy="50"
                  r="42"
                  fill="none"
                  stroke-width="10"
                  stroke-linecap="round"
                  class="stroke-indigo-600 transition-all duration-500"
                  :stroke-dasharray="ringCircumference"
                  :stroke-dashoffset="ringDashOffset"
                />
              </svg>
              <div class="absolute inset-0 flex flex-col items-center justify-center">
                <span class="text-lg font-bold text-slate-900">{{ coveragePct.toFixed(1) }}%</span>
                <span class="text-[10px] font-medium text-slate-400 uppercase">{{ t('corr.coveredBadge') }}</span>
              </div>
            </div>

            <div class="min-w-0 flex-1 space-y-2 text-xs">
              <div class="flex items-center gap-1.5 font-semibold text-slate-900">
                <Layers class="h-4 w-4 text-indigo-600" />
                <span>{{ t('corr.diffTitle') }}</span>
              </div>
              <div class="grid grid-cols-2 gap-2">
                <div class="rounded-lg bg-slate-50 px-2.5 py-1.5">
                  <div class="text-slate-400">{{ t('corr.metric.total') }}</div>
                  <div class="text-sm font-bold text-slate-900">{{ coverage.total_endpoints }}</div>
                </div>
                <div class="rounded-lg bg-emerald-50 px-2.5 py-1.5">
                  <div class="text-emerald-700">{{ t('corr.metric.liveHar') }}</div>
                  <div class="text-sm font-bold text-emerald-800">{{ coverage.covered_count }}</div>
                </div>
                <div class="rounded-lg bg-amber-50 px-2.5 py-1.5">
                  <div class="text-amber-700">{{ t('corr.metric.jsInferred') }}</div>
                  <div class="text-sm font-bold text-amber-800">{{ coverage.js_static_inferred_uncovered_count }}</div>
                </div>
                <div class="rounded-lg bg-rose-50 px-2.5 py-1.5">
                  <div class="text-rose-700">{{ t('corr.metric.uncovered') }}</div>
                  <div class="text-sm font-bold text-rose-800">{{ coverage.uncovered_count }}</div>
                </div>
              </div>
            </div>
          </div>

          <!-- Prefix Coverage Table -->
          <div class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm lg:col-span-2">
            <div class="border-b border-slate-200 px-5 py-3.5">
              <h3 class="text-sm font-semibold text-slate-900">{{ t('corr.prefixTitle') }}</h3>
            </div>
            <div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-slate-200 text-left text-xs">
                <thead class="bg-slate-50 font-semibold text-slate-500 uppercase">
                  <tr>
                    <th class="px-4 py-2.5">{{ t('corr.col.prefix') }}</th>
                    <th class="px-3 py-2.5 text-right">{{ t('corr.col.total') }}</th>
                    <th class="px-3 py-2.5 text-right">{{ t('corr.col.covered') }}</th>
                    <th class="px-3 py-2.5 text-right">{{ t('corr.col.uncovered') }}</th>
                    <th class="w-44 px-4 py-2.5">{{ t('corr.col.coveragePct') }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-slate-100">
                  <tr v-for="row in coverage.by_prefix" :key="row.prefix">
                    <td class="px-4 py-2.5 font-mono font-medium text-slate-800">{{ row.prefix }}</td>
                    <td class="px-3 py-2.5 text-right font-mono text-slate-600">{{ row.total }}</td>
                    <td class="px-3 py-2.5 text-right font-mono text-emerald-700">{{ row.covered }}</td>
                    <td class="px-3 py-2.5 text-right font-mono text-rose-600">{{ row.uncovered }}</td>
                    <td class="px-4 py-2.5">
                      <div class="flex items-center gap-2">
                        <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                          <div
                            class="h-full rounded-full bg-indigo-600"
                            :style="{ width: `${Math.min(100, row.coverage_pct)}%` }"
                          />
                        </div>
                        <span class="w-11 text-right font-mono text-slate-700">{{ row.coverage_pct.toFixed(0) }}%</span>
                      </div>
                    </td>
                  </tr>
                  <tr v-if="coverage.by_prefix.length === 0">
                    <td colspan="5" class="px-4 py-6 text-center text-slate-400">
                      {{ t('corr.noPrefixes') }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- Uncovered Blind-Spot List -->
        <div class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-5 py-3.5">
            <div class="flex items-center gap-2">
              <ShieldAlert class="h-4 w-4 text-amber-600" />
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t('corr.blindSpotsTitle', { count: coverage.uncovered_endpoints.length }) }}
              </h3>
            </div>
            <span class="text-xs text-slate-500">
              {{ t('corr.blindSpotsHint') }}
            </span>
          </div>

          <div
            v-if="coverage.uncovered_endpoints.length === 0"
            class="px-5 py-8 text-center text-xs text-emerald-700"
          >
            {{ t('corr.allCovered') }}
          </div>

          <div v-else class="divide-y divide-slate-100">
            <div
              v-for="ep in coverage.uncovered_endpoints"
              :key="`${ep.method}-${ep.path}`"
              class="flex flex-col gap-2 px-5 py-3.5 sm:flex-row sm:items-center sm:justify-between"
            >
              <div class="min-w-0 space-y-1">
                <div class="flex flex-wrap items-center gap-2">
                  <StatusBadge :status="ep.priority" size="sm" />
                  <StatusBadge :status="ep.source" size="sm" />
                  <span class="font-mono text-xs font-bold text-slate-700">{{ ep.method }}</span>
                  <span class="font-mono text-xs font-medium text-slate-900">{{ ep.path }}</span>
                </div>
                <div v-if="ep.inferred_params.length > 0" class="flex flex-wrap items-center gap-1 text-[11px] text-slate-500">
                  <span>{{ t('corr.inferredParams') }}</span>
                  <span
                    v-for="param in ep.inferred_params"
                    :key="param"
                    class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-700"
                  >
                    {{ param }}
                  </span>
                </div>
                <pre
                  v-if="ep.js_snippet"
                  class="mt-1 max-h-24 overflow-auto rounded bg-slate-900 px-2.5 py-1.5 font-mono text-[11px] text-amber-200"
                >{{ ep.js_snippet }}</pre>
              </div>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>
