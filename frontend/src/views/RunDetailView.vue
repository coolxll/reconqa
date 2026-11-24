<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Archive,
  ArrowLeft,
  Check,
  CheckCircle2,
  Circle,
  Clock,
  Coffee,
  Copy,
  Download,
  FileCode2,
  FileUp,
  Gauge,
  GitBranch,
  Loader2,
  Play,
  RefreshCw,
  ShieldAlert,
  Terminal,
  Trash2,
  XCircle,
} from 'lucide-vue-next'
import type { Job, Run } from '@/api/types'
import { artifactDownloadUrl } from '@/api/client'
import { deleteRun, getRun, getRunTitle, listProjectRuns, shortRunId, startRun } from '@/api/runs'
import { useWorkspace } from '@/stores/workspace'
import StatusBadge from '@/components/layout/StatusBadge.vue'
import MetricCard from '@/components/layout/MetricCard.vue'
import EmptyState from '@/components/layout/EmptyState.vue'
import HarUploadModal from '@/components/runs/HarUploadModal.vue'
import SseLogConsole from '@/components/runs/SseLogConsole.vue'
import OpenApiExplorer from '@/components/runs/OpenApiExplorer.vue'
import CorrelationPanel from '@/components/runs/CorrelationPanel.vue'
import ArtifactsPanel from '@/components/runs/ArtifactsPanel.vue'
import ExternalToolRunnerCard from '@/components/runs/ExternalToolRunnerCard.vue'
import '@/i18n/locales/runs'
import { useI18n } from '@/i18n'

const { locale, t } = useI18n()

// Peer dashboard boards built by fe-dashboards (prop strictly runId: string)
const JavaAllureBoard = defineAsyncComponent(
  () => import('@/components/dashboards/JavaAllureBoard.vue'),
)
const SecurityBoard = defineAsyncComponent(
  () => import('@/components/dashboards/SecurityBoard.vue'),
)
const PerformanceBoard = defineAsyncComponent(
  () => import('@/components/dashboards/PerformanceBoard.vue'),
)

const PIPELINE_STEPS = computed(() => [
  { id: 'discovery', label: t('runDetail.step.discovery') },
  { id: 'merge', label: t('runDetail.step.merge') },
  { id: 'java_gen', label: t('runDetail.step.java_gen') },
  { id: 'perf', label: t('runDetail.step.perf') },
  { id: 'security', label: t('runDetail.step.security') },
])

const VALID_TAB_IDS = [
  'overview',
  'openapi',
  'correlation',
  'java',
  'performance',
  'security',
  'artifacts',
] as const

type TabId = (typeof VALID_TAB_IDS)[number]

const TABS = computed(() => [
  { id: 'overview' as TabId, label: t('runDetail.tab.overview'), icon: Terminal, pillar: '' },
  { id: 'openapi' as TabId, label: t('runDetail.tab.openapi'), icon: FileCode2, pillar: 'hub' },
  { id: 'correlation' as TabId, label: t('runDetail.tab.correlation'), icon: GitBranch, pillar: 'hub' },
  { id: 'java' as TabId, label: t('runDetail.tab.java'), icon: Coffee, pillar: 'indigo' },
  { id: 'performance' as TabId, label: t('runDetail.tab.performance'), icon: Gauge, pillar: 'emerald' },
  { id: 'security' as TabId, label: t('runDetail.tab.security'), icon: ShieldAlert, pillar: 'rose' },
  { id: 'artifacts' as TabId, label: t('runDetail.tab.artifacts'), icon: Archive, pillar: '' },
])

const route = useRoute()
const router = useRouter()
const { activeProjectId, setActiveProject } = useWorkspace()

const runId = computed(() => String(route.params.runId ?? ''))
const runsBackPath = computed(() => {
  const pid =
    run.value?.project_id ||
    (typeof route.params.projectId === 'string' && route.params.projectId) ||
    activeProjectId.value
  return pid ? `/p/${pid}/runs` : '/runs'
})

function resolveValidTab(raw: unknown): TabId {
  const str = typeof raw === 'string' ? raw : ''
  return (VALID_TAB_IDS as readonly string[]).includes(str) ? (str as TabId) : 'overview'
}

const activeTab = ref<TabId>(resolveValidTab(route.query.tab))
const loading = ref(true)
const starting = ref(false)
const deleting = ref(false)
const confirmingDelete = ref(false)
const errorMessage = ref('')
const run = ref<Run | null>(null)
const siblingRuns = ref<Run[]>([])
const harModalOpen = ref(false)
const copiedId = ref(false)

watch(
  () => route.query.tab,
  (nextTab) => {
    activeTab.value = resolveValidTab(nextTab)
  },
)

function selectTab(tab: TabId) {
  activeTab.value = tab
  router.replace({
    query: { ...route.query, tab },
  })
}

async function reloadRun() {
  if (!runId.value) return
  try {
    const data = await getRun(runId.value)
    run.value = data
    errorMessage.value = ''
    if (data.project_id) {
      setActiveProject(data.project_id)
      try {
        siblingRuns.value = await listProjectRuns(data.project_id)
      } catch {
        // non-critical
      }
    }
  } catch (err) {
    errorMessage.value =
      err instanceof Error ? err.message : t('runDetail.loadError')
  } finally {
    loading.value = false
  }
}

async function handleStartRun() {
  if (!runId.value || starting.value) return
  starting.value = true
  errorMessage.value = ''
  try {
    run.value = await startRun(runId.value)
  } catch (err) {
    errorMessage.value =
      err instanceof Error ? err.message : t('runDetail.startError')
  } finally {
    starting.value = false
  }
}

async function handleDeleteRun() {
  if (!runId.value || deleting.value) return
  deleting.value = true
  errorMessage.value = ''
  try {
    await deleteRun(runId.value)
    await router.push(runsBackPath.value)
  } catch (err) {
    errorMessage.value =
      err instanceof Error ? err.message : t('runDetail.deleteError')
    confirmingDelete.value = false
  } finally {
    deleting.value = false
  }
}

function jobTargetTab(moduleName: string): TabId | null {
  switch (moduleName) {
    case 'merge':
      return 'openapi'
    case 'java_gen':
      return 'java'
    case 'security':
      return 'security'
    case 'perf':
      return 'performance'
    default:
      return null
  }
}

function asRecord(val: unknown): Record<string, unknown> | null {
  return val && typeof val === 'object' && !Array.isArray(val)
    ? (val as Record<string, unknown>)
    : null
}

function readNum(...candidates: unknown[]): number | null {
  for (const c of candidates) {
    if (typeof c === 'number' && !Number.isNaN(c)) return c
    if (Array.isArray(c)) return c.length
  }
  return null
}

function formatJobSummary(job: Job): { text: string; isError?: boolean } {
  const meta = asRecord(job.output_meta)
  if (!meta || Object.keys(meta).length === 0) {
    return { text: '—' }
  }
  if (typeof meta.error === 'string' && meta.error.trim()) {
    return { text: meta.error.trim().slice(0, 140), isError: true }
  }
  if (meta.skipped === true) {
    const reason = typeof meta.reason === 'string' ? `: ${meta.reason}` : ''
    return { text: `${t('runDetail.summary.skipped')}${reason}` }
  }

  const parts: string[] = []
  if (job.module === 'merge') {
    const cov = asRecord(meta.coverage)
    const eps = readNum(meta.openapi_paths, cov?.total_endpoints)
    const pct = readNum(cov?.coverage_pct)
    const chains = readNum(meta.correlation_chains)
    const vars = readNum(meta.correlation_variables)
    if (eps !== null) parts.push(t('runDetail.summary.endpoints', { n: eps }))
    if (pct !== null) parts.push(t('runDetail.summary.coverage', { pct: pct.toFixed(1) }))
    if (chains !== null && chains > 0) parts.push(t('runDetail.summary.chains', { n: chains }))
    else if (vars !== null && vars > 0) parts.push(t('runDetail.summary.vars', { n: vars }))
  } else if (job.module === 'java_gen') {
    const l1 = readNum(meta.l1_endpoints_count, meta.l1_count)
    const l2 = readNum(meta.l2_chains_count, meta.l2_chain_count)
    const l3 = readNum(meta.l3_auth_endpoints_count, meta.l3_count)
    const files = readNum(meta.files_generated)
    if (l1 !== null) parts.push(t('runDetail.summary.javaL1', { n: l1 }))
    if (l2 !== null) parts.push(t('runDetail.summary.javaL2', { n: l2 }))
    if (l3 !== null) parts.push(t('runDetail.summary.javaL3', { n: l3 }))
    if (parts.length === 0 && files !== null) {
      parts.push(t('runDetail.summary.javaFiles', { n: files }))
    }
  } else if (job.module === 'security') {
    const stat = asRecord(meta.static_audit)
    const statSummary = asRecord(stat?.summary)
    const auth = asRecord(meta.auth_matrix)
    const authSummary = asRecord(auth?.summary)
    const burp = asRecord(meta.burp_export)
    const nuclei = asRecord(meta.nuclei)

    const staticCount = readNum(statSummary?.findings_count, stat?.findings)
    const idorCount =
      (readNum(auth?.confirmed_idor_count, authSummary?.confirmed_idor_count) ?? 0) +
      (readNum(auth?.suspected_idor_count, authSummary?.suspected_idor_count) ?? 0) +
      (readNum(auth?.unauth_bypass_count, authSummary?.unauth_bypass_count) ?? 0)
    const burpCount = readNum(burp?.exported_count, burp?.count, burp?.files)
    const nucleiCount = readNum(nuclei?.findings_count, nuclei?.findings)

    if (staticCount !== null) parts.push(t('runDetail.summary.secFindings', { n: staticCount }))
    if (idorCount > 0) parts.push(t('runDetail.summary.secIdor', { n: idorCount }))
    if (burpCount !== null) parts.push(t('runDetail.summary.secBurp', { n: burpCount }))
    if (nucleiCount !== null && nucleiCount > 0) {
      parts.push(t('runDetail.summary.secNuclei', { n: nucleiCount }))
    }
  } else if (job.module === 'perf') {
    const hp = asRecord(meta.har_profiler)
    const hpSummary = asRecord(hp?.summary)
    const reqs = readNum(hpSummary?.total_requests, hp?.total_requests)
    const p95 = readNum(hpSummary?.p95_total_ms)
    const findingsCount = readNum(hpSummary?.findings_count, hp?.findings)
    const largeCount = readNum(hp?.largest_responses)
    if (reqs !== null) parts.push(t('runDetail.summary.perfReqs', { n: reqs }))
    if (p95 !== null) parts.push(t('runDetail.summary.perfP95', { ms: Math.round(p95) }))
    if (findingsCount !== null) parts.push(t('runDetail.summary.perfFindings', { n: findingsCount }))
    else if (largeCount !== null && largeCount > 0) {
      parts.push(t('runDetail.summary.perfLarge', { n: largeCount }))
    }
  } else if (job.module === 'discovery') {
    const subs = readNum(meta.subdomains_count, meta.subdomains)
    const hosts = readNum(meta.live_hosts_count, meta.live_hosts)
    const routes = readNum(meta.spa_routes_count, meta.spa_routes)
    if (subs !== null) parts.push(t('runDetail.summary.discSubdomains', { n: subs }))
    if (hosts !== null) parts.push(t('runDetail.summary.discHosts', { n: hosts }))
    if (routes !== null) parts.push(t('runDetail.summary.discRoutes', { n: routes }))
  }

  if (parts.length === 0) {
    return { text: job.status === 'completed' ? t('runDetail.summary.completed') : '—' }
  }
  return { text: parts.join(' · ') }
}

async function copyRunId() {
  if (!runId.value) return
  try {
    await navigator.clipboard.writeText(runId.value)
    copiedId.value = true
    window.setTimeout(() => {
      copiedId.value = false
    }, 1500)
  } catch {
    // ignore
  }
}

const jobsByModule = computed(() => {
  const map = new Map<string, Job>()
  for (const job of run.value?.jobs ?? []) {
    map.set(job.module, job)
  }
  return map
})

const pipelineStepperItems = computed(() => {
  const configured = Array.isArray(run.value?.config_snapshot?.modules)
    ? new Set((run.value?.config_snapshot?.modules as unknown[]).map(String))
    : new Set((run.value?.jobs ?? []).map((j) => j.module))

  return PIPELINE_STEPS.value.map((step) => {
    const job = jobsByModule.value.get(step.id)
    const enabled = configured.has(step.id) || Boolean(job)
    return {
      ...step,
      enabled,
      status: job?.status ?? (enabled ? 'pending' : 'skipped'),
      job,
    }
  })
})

function metricVal(keys: string[], fallback = '—'): string | number {
  const m = run.value?.summary_metrics
  if (!m) return fallback
  for (const k of keys) {
    const v = m[k]
    if (typeof v === 'number' && !Number.isNaN(v)) return v
    if (typeof v === 'string' && v.length > 0) return v
  }
  return fallback
}

const coverageDisplay = computed(() => {
  const raw = metricVal(['coverage_pct'], '')
  if (typeof raw === 'number') return `${raw.toFixed(1)}%`
  return raw ? `${raw}%` : '—'
})

function formatDuration(startIso?: string | null, endIso?: string | null): string {
  if (!startIso) return '—'
  const start = new Date(startIso).getTime()
  const end = endIso ? new Date(endIso).getTime() : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '—'
  const secs = Math.max(0, Math.round((end - start) / 1000))
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  return `${mins}m ${secs % 60}s`
}

function formatDateTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(locale.value === 'zh' ? 'zh-CN' : 'en-US')
}

function stepCardClass(status: string, enabled: boolean): string {
  if (!enabled || status === 'skipped') {
    return 'border-slate-200 bg-slate-50/70 text-slate-400'
  }
  if (status === 'completed' || status === 'success') {
    return 'border-emerald-200 bg-emerald-50/60 text-emerald-800'
  }
  if (status === 'running') {
    return 'border-indigo-300 bg-indigo-50 text-indigo-800 ring-1 ring-indigo-500/20'
  }
  if (status === 'failed') {
    return 'border-rose-200 bg-rose-50 text-rose-800'
  }
  return 'border-slate-200 bg-white text-slate-700'
}

watch(runId, () => {
  loading.value = true
  reloadRun()
})

onMounted(() => {
  reloadRun()
})
</script>

<template>
  <div class="space-y-6">
    <!-- Loading -->
    <div
      v-if="loading && !run"
      class="flex items-center justify-center rounded-xl border border-slate-200 bg-white py-20 text-sm text-slate-500"
    >
      {{ t('runDetail.loading') }}
    </div>

    <EmptyState
      v-else-if="!run"
      :title="t('runDetail.notFoundTitle')"
      :description="errorMessage || t('runDetail.notFoundDesc', { runId })"
    >
      <router-link
        :to="runsBackPath"
        class="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
      >
        <ArrowLeft class="h-4 w-4" />
        {{ t('runDetail.backToRuns') }}
      </router-link>
    </EmptyState>

    <template v-else>
      <!-- Header Card -->
      <div class="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div class="space-y-1.5">
            <div class="flex flex-wrap items-center gap-2.5">
              <router-link
                :to="runsBackPath"
                class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-500 transition hover:bg-slate-100 hover:text-slate-800"
              >
                <ArrowLeft class="h-3.5 w-3.5" />
                {{ t('runDetail.breadcrumbRuns') }}
              </router-link>
              <span class="text-slate-300">/</span>
              <h1 class="text-base font-bold text-slate-900">
                {{ getRunTitle(run) }}
              </h1>
              <span class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-500" :title="run.id">
                {{ shortRunId(run.id) }}
              </span>
              <button
                type="button"
                class="rounded p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
                :title="t('runDetail.copyRunId')"
                @click="copyRunId"
              >
                <Check v-if="copiedId" class="h-3.5 w-3.5 text-emerald-600" />
                <Copy v-else class="h-3.5 w-3.5" />
              </button>
              <StatusBadge :status="run.status" size="md" />
            </div>

            <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
              <span>{{ t('runDetail.trigger') }} <strong class="font-mono uppercase text-slate-700">{{ run.trigger_source }}</strong></span>
              <span v-if="run.config_snapshot?.target_url">
                {{ t('runDetail.target') }} <strong class="font-mono text-slate-700">{{ run.config_snapshot.target_url }}</strong>
              </span>
              <span>{{ t('runDetail.started') }} <strong class="text-slate-700">{{ formatDateTime(run.started_at) }}</strong></span>
              <span>{{ t('runDetail.duration') }} <strong class="font-mono text-slate-700">{{ formatDuration(run.started_at, run.finished_at) }}</strong></span>
            </div>
          </div>

          <!-- Action & Quick Download Buttons -->
          <div class="flex flex-wrap items-center gap-2">
            <button
              v-if="run.status === 'pending' || run.status === 'failed'"
              type="button"
              :disabled="starting"
              class="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-2 text-xs font-semibold text-white shadow-sm transition hover:bg-indigo-700 disabled:opacity-50"
              @click="handleStartRun"
            >
              <RefreshCw v-if="starting" class="h-3.5 w-3.5 animate-spin" />
              <Play v-else class="h-3.5 w-3.5" />
              <span>{{ run.status === 'failed' ? t('runDetail.retryRun') : t('runDetail.startRun') }}</span>
            </button>

            <button
              v-if="run.status !== 'running'"
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
              @click="harModalOpen = true"
            >
              <FileUp class="h-3.5 w-3.5 text-indigo-600" />
              <span>{{ t('runDetail.attachTraffic') }}</span>
            </button>

            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
              @click="reloadRun"
            >
              <RefreshCw class="h-3.5 w-3.5" />
              <span>{{ t('runDetail.refresh') }}</span>
            </button>

            <div class="hidden h-5 w-px bg-slate-200 sm:block" />

            <a
              :href="artifactDownloadUrl(run.id, 'java-tests')"
              download
              class="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50/70 px-3 py-2 text-xs font-medium text-indigo-700 transition hover:bg-indigo-100"
            >
              <Download class="h-3.5 w-3.5" />
              <span>{{ t('runDetail.downloadJava') }}</span>
            </a>

            <a
              :href="artifactDownloadUrl(run.id, 'openapi_yaml')"
              download
              class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
            >
              <Download class="h-3.5 w-3.5" />
              <span>{{ t('runDetail.downloadOpenApi') }}</span>
            </a>

            <a
              :href="artifactDownloadUrl(run.id, 'k6_script')"
              download
              class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
            >
              <Download class="h-3.5 w-3.5" />
              <span>{{ t('runDetail.downloadK6') }}</span>
            </a>

            <div class="hidden h-5 w-px bg-slate-200 sm:block" />

            <template v-if="confirmingDelete">
              <button
                type="button"
                data-testid="run-detail-delete-confirm"
                :disabled="deleting"
                class="inline-flex items-center gap-1 rounded-lg bg-rose-600 px-3 py-2 text-xs font-semibold text-white transition hover:bg-rose-700 disabled:opacity-50"
                @click="handleDeleteRun"
              >
                <span>{{ t('runDetail.deleteConfirm') }}</span>
              </button>
              <button
                type="button"
                class="inline-flex items-center rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-xs font-medium text-slate-600 hover:bg-slate-50"
                @click="confirmingDelete = false"
              >
                {{ t('runDetail.deleteCancel') }}
              </button>
            </template>
            <button
              v-else
              type="button"
              data-testid="run-detail-delete-btn"
              :disabled="run.status === 'running'"
              class="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-medium text-rose-700 transition hover:bg-rose-100 disabled:cursor-not-allowed disabled:opacity-40"
              @click="confirmingDelete = true"
            >
              <Trash2 class="h-3.5 w-3.5" />
              <span>{{ t('runDetail.deleteRun') }}</span>
            </button>
          </div>
        </div>

        <!-- Error notice -->
        <div
          v-if="errorMessage"
          class="mt-4 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2 text-xs text-rose-700"
        >
          {{ errorMessage }}
        </div>

        <!-- Job Status Pipeline Stepper -->
        <div class="mt-5 grid grid-cols-2 gap-2.5 sm:grid-cols-5">
          <div
            v-for="(step, idx) in pipelineStepperItems"
            :key="step.id"
            class="relative flex items-center justify-between rounded-lg border px-3 py-2.5 text-xs transition"
            :class="stepCardClass(step.status, step.enabled)"
          >
            <div class="flex min-w-0 items-center gap-2">
              <CheckCircle2
                v-if="step.status === 'completed' || step.status === 'success'"
                class="h-4 w-4 shrink-0 text-emerald-600"
              />
              <Loader2
                v-else-if="step.status === 'running'"
                class="h-4 w-4 shrink-0 animate-spin text-indigo-600"
              />
              <XCircle
                v-else-if="step.status === 'failed'"
                class="h-4 w-4 shrink-0 text-rose-600"
              />
              <Clock
                v-else-if="step.status === 'queued'"
                class="h-4 w-4 shrink-0 text-amber-500"
              />
              <Circle v-else class="h-4 w-4 shrink-0 opacity-40" />

              <div class="min-w-0">
                <div class="truncate font-semibold">
                  {{ idx + 1 }}. {{ step.label }}
                </div>
                <div class="truncate text-[10px] capitalize opacity-75">
                  {{ step.enabled ? step.status : t('runDetail.step.notSelected') }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 7 Clean Tabs Navigation -->
      <div class="border-b border-slate-200">
        <nav class="-mb-px flex items-center gap-1 overflow-x-auto" aria-label="Run detail tabs">
          <button
            v-for="tab in TABS"
            :key="tab.id"
            type="button"
            class="inline-flex shrink-0 items-center gap-2 border-b-2 px-3.5 py-2.5 text-sm font-medium transition"
            :class="
              activeTab === tab.id
                ? tab.pillar === 'emerald'
                  ? 'border-emerald-600 text-emerald-700'
                  : tab.pillar === 'rose'
                    ? 'border-rose-600 text-rose-700'
                    : 'border-indigo-600 text-indigo-700'
                : 'border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-800'
            "
            @click="selectTab(tab.id)"
          >
            <component :is="tab.icon" class="h-4 w-4" />
            <span>{{ tab.label }}</span>
            <span
              v-if="tab.pillar === 'indigo' || tab.pillar === 'emerald' || tab.pillar === 'rose'"
              class="h-1.5 w-1.5 rounded-full"
              :class="
                tab.pillar === 'emerald'
                  ? 'bg-emerald-500'
                  : tab.pillar === 'rose'
                    ? 'bg-rose-500'
                    : 'bg-indigo-500'
              "
            />
          </button>
        </nav>
      </div>

      <!-- Tab Panels -->
      <div v-if="activeTab === 'overview'" class="space-y-6">
        <!-- Quick Summary Metrics -->
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            :label="t('runDetail.kpi.endpoints')"
            :value="metricVal(['total_endpoints', 'endpoints_count', 'endpoints'])"
            :subtitle="t('runDetail.kpi.endpointsSub')"
            tone="indigo"
          />
          <MetricCard
            :label="t('runDetail.kpi.coverage')"
            :value="coverageDisplay"
            :subtitle="t('runDetail.kpi.coverageSub')"
            tone="emerald"
          />
          <MetricCard
            :label="t('runDetail.kpi.chains')"
            :value="metricVal(['correlation_chains', 'chains_count'])"
            :subtitle="t('runDetail.kpi.chainsSub')"
            tone="neutral"
          />
          <MetricCard
            :label="t('runDetail.kpi.findings')"
            :value="metricVal(['findings_total', 'findings_count', 'total_findings', 'security_findings_count'])"
            :subtitle="t('runDetail.kpi.findingsSub')"
            tone="amber"
          />
        </div>

        <!-- Job Pipeline Timeline Table -->
        <div class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div class="border-b border-slate-200 px-5 py-3.5">
            <h3 class="text-sm font-semibold text-slate-900">{{ t('runDetail.timeline.title') }}</h3>
          </div>
          <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-slate-200 text-left text-xs">
              <thead class="bg-slate-50 font-semibold text-slate-500 uppercase">
                <tr>
                  <th class="px-5 py-2.5">{{ t('runDetail.timeline.module') }}</th>
                  <th class="px-4 py-2.5">{{ t('runDetail.timeline.status') }}</th>
                  <th class="px-4 py-2.5">{{ t('runDetail.timeline.started') }}</th>
                  <th class="px-4 py-2.5">{{ t('runDetail.timeline.duration') }}</th>
                  <th class="px-5 py-2.5">{{ t('runDetail.timeline.output') }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100">
                <tr v-for="job in run.jobs" :key="job.id" class="hover:bg-slate-50/70">
                  <td class="px-5 py-3 font-mono font-semibold text-slate-800">{{ job.module }}</td>
                  <td class="px-4 py-3">
                    <StatusBadge :status="job.status" size="sm" />
                  </td>
                  <td class="px-4 py-3 text-slate-500">{{ formatDateTime(job.started_at) }}</td>
                  <td class="px-4 py-3 font-mono text-slate-700">
                    {{ formatDuration(job.started_at, job.finished_at) }}
                  </td>
                  <td class="px-5 py-3 text-xs">
                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <span
                        data-testid="job-output-summary"
                        :class="
                          formatJobSummary(job).isError
                            ? 'text-rose-600 font-medium'
                            : 'text-slate-700'
                        "
                      >
                        {{ formatJobSummary(job).text }}
                      </span>
                      <button
                        v-if="job.status === 'completed' && jobTargetTab(job.module)"
                        type="button"
                        class="shrink-0 font-medium text-indigo-600 transition hover:text-indigo-800 hover:underline"
                        @click="selectTab(jobTargetTab(job.module)!)"
                      >
                        {{ t('runDetail.timeline.viewTab') }}
                      </button>
                    </div>
                  </td>
                </tr>
                <tr v-if="run.jobs.length === 0">
                  <td colspan="5" class="px-5 py-6 text-center text-slate-400">
                    {{ t('runDetail.timeline.empty') }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Real External Tool Runner & Scenario Sync-Back -->
        <ExternalToolRunnerCard
          :project-id="run.project_id"
          :run-id="run.id"
          @executed="reloadRun"
        />

        <!-- Live SSE Console -->
        <SseLogConsole
          :run-id="run.id"
          :status="run.status"
          @status-change="reloadRun"
        />
      </div>

      <OpenApiExplorer
        v-else-if="activeTab === 'openapi'"
        :run-id="run.id"
        :project-id="run.project_id"
      />

      <CorrelationPanel
        v-else-if="activeTab === 'correlation'"
        :run-id="run.id"
        :project-id="run.project_id"
      />

      <JavaAllureBoard
        v-else-if="activeTab === 'java'"
        :run-id="run.id"
        :project-id="run.project_id"
        @executed="reloadRun"
      />

      <SecurityBoard
        v-else-if="activeTab === 'security'"
        :run-id="run.id"
        :project-id="run.project_id"
        @executed="reloadRun"
      />

      <PerformanceBoard
        v-else-if="activeTab === 'performance'"
        :run-id="run.id"
        :project-id="run.project_id"
        @executed="reloadRun"
      />

      <ArtifactsPanel v-else-if="activeTab === 'artifacts'" :run-id="run.id" />

      <!-- HAR / cURL Upload / Reuse Modal -->
      <HarUploadModal
        :open="harModalOpen"
        :run-id="run.id"
        :project-id="run.project_id"
        :previous-runs="siblingRuns"
        @close="harModalOpen = false"
        @uploaded="reloadRun"
        @started="reloadRun"
      />
    </template>
  </div>
</template>
