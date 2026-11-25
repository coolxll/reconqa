<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import DOMPurify from 'dompurify'
import mermaid from 'mermaid'
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Code2,
  Copy,
  Database,
  Eye,
  GitBranch,
  History,
  Layers,
  Loader2,
  Play,
  RotateCcw,
  Search,
  ShieldAlert,
  SkipForward,
  Terminal,
  XCircle,
} from 'lucide-vue-next'
import type {
  AuthProfile,
  ChainRunOut,
  ChainStepResult,
  CorrelationChain,
  CorrelationReport,
  CorrelationVariable,
  CoverageDiffReport,
  ScenarioItem,
  ScenarioPriority,
  UncoveredEndpoint,
} from '@/api/types'
import { getRunCorrelation, getRunCoverage } from '@/api/runs'
import { listAuthProfiles } from '@/api/projects'
import { listProjectScenarios } from '@/api/scenarios'
import { executeChain, listChainRuns, resumeChainRun } from '@/api/chains'
import { computeResumeStepIndex, matchesChainCheckpoint } from '@/utils/assertionOperators'
import { hasSyncFeedback } from '@/utils/gateAndExport'
import { useWorkspace } from '@/stores/workspace'
import StatusBadge from '@/components/layout/StatusBadge.vue'
import EmptyState from '@/components/layout/EmptyState.vue'
import '@/i18n/locales/runs'
import { useI18n } from '@/i18n'

const { t } = useI18n()
const { activeProjectId } = useWorkspace()

const props = defineProps<{
  runId: string
  projectId?: string
}>()

const emit = defineEmits<{
  synced: [payload: { synced_scenarios: number; synced_assertions: number }]
}>()

interface ScenarioMeta {
  id: string
  slug: string
  name: string
  priority: ScenarioPriority
  effectiveness_level: string
}

interface ScenarioFilterOption {
  slug: string
  label: string
  priority: ScenarioPriority | null
  scenarioId: string | null
  linkCount: number
}

interface UncoveredPrefixGroup {
  prefix: string
  endpoints: UncoveredEndpoint[]
  methods: Array<{ method: string; count: number }>
}

const INITIAL_GROUP_LIMIT = 20
const LOAD_MORE_STEP = 50

const loading = ref(true)
const correlation = ref<CorrelationReport | null>(null)
const coverage = ref<CoverageDiffReport | null>(null)
const scenarioMap = ref<Record<string, ScenarioMeta>>({})
const selectedScenario = ref<string>('ALL')
const renderedSvg = ref('')
const mermaidError = ref('')
const showRawMermaid = ref(false)
const copiedMermaid = ref(false)

// Uncovered endpoints search & accordion state (collapsed by default)
const uncoveredSearch = ref('')
const expandedPrefixes = ref<Record<string, boolean>>({})
const visibleLimitByPrefix = ref<Record<string, number>>({})

// Lightweight Chain Runner & Checkpoint/Resume state
const authProfiles = ref<AuthProfile[]>([])
const chainRuns = ref<ChainRunOut[]>([])
const expandedRunners = ref<Record<string, boolean>>({})
const credentialByChain = ref<Record<string, string>>({})
const baseUrlByChain = ref<Record<string, string>>({})
const dbDsnRefByChain = ref<Record<string, string>>({})
const initialVarsByChain = ref<Record<string, string>>({})
const runningByChain = ref<Record<string, boolean>>({})
const resumingByChain = ref<Record<string, boolean>>({})
const chainErrorByChain = ref<Record<string, string>>({})
const activeChainRunByChain = ref<Record<string, ChainRunOut | null>>({})
const resumeFromIndexByChain = ref<Record<string, number>>({})
const overrideVarsByChain = ref<Record<string, string>>({})
const overrideBodyByChain = ref<Record<string, string>>({})
const expandedStepPreviews = ref<Record<string, boolean>>({})
const syncScenariosByChain = ref<Record<string, boolean>>({})

let mermaidReady = false
let renderCounter = 0
let loadSeq = 0
let runnerSeq = 0

const effectiveProjectId = computed(() => props.projectId || activeProjectId.value || '')

function initMermaidOnce() {
  if (mermaidReady) return
  mermaid.initialize({
    startOnLoad: false,
    theme: 'neutral',
    securityLevel: 'strict',
    htmlLabels: false,
    flowchart: {
      useMaxWidth: true,
      htmlLabels: false,
      curve: 'basis',
    },
  })
  mermaidReady = true
}

function escapeMermaidLabel(raw: string): string {
  return raw
    .replace(/[\r\n]+/g, ' ')
    .replace(/"/g, "'")
    .replace(/[<>]/g, '')
    .trim()
}

function buildFocusedMermaid(variables: CorrelationVariable[]): string {
  if (variables.length === 0) return ''

  const nodeIds = new Map<string, string>()
  const nodeLines: string[] = []
  const edgeLines: string[] = []
  const seenEdges = new Set<string>()

  const ensureNode = (step: number, method: string, path: string, scenario: string): string => {
    const key = `${scenario}::${step}::${method.toUpperCase()}::${path}`
    const existing = nodeIds.get(key)
    if (existing) return existing
    const id = `N${nodeIds.size}`
    nodeIds.set(key, id)
    const shortPath = path.length > 44 ? `${path.slice(0, 41)}...` : path
    const label = escapeMermaidLabel(`#${step} ${method.toUpperCase()} ${shortPath}`)
    nodeLines.push(`  ${id}["${label}"]`)
    return id
  }

  for (const variable of variables) {
    const prod = variable.producer
    const prodId = ensureNode(prod.step, prod.method, prod.path, prod.scenario || '')
    const edgeLabel = escapeMermaidLabel(`{{${variable.var_name}}}`)

    for (const consumer of variable.consumers) {
      const consId = ensureNode(consumer.step, consumer.method, consumer.path, consumer.scenario || '')
      const edgeKey = `${prodId}->${consId}::${variable.var_name}`
      if (seenEdges.has(edgeKey)) continue
      seenEdges.add(edgeKey)
      edgeLines.push(`  ${prodId} -->|"${edgeLabel}"| ${consId}`)
    }
  }

  if (edgeLines.length === 0) return ''
  return ['flowchart LR', ...nodeLines, ...edgeLines].join('\n')
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
    renderedSvg.value = DOMPurify.sanitize(svg, {
      USE_PROFILES: { svg: true, svgFilters: true },
    })
  } catch (err) {
    mermaidError.value =
      err instanceof Error ? err.message : 'Unable to render Mermaid SVG diagram.'
    showRawMermaid.value = true
  }
}

function getScenarioMeta(slug: string | null | undefined): ScenarioMeta | null {
  if (!slug) return null
  return scenarioMap.value[slug] ?? null
}

function scenarioDisplayName(slug: string | null | undefined): string {
  if (!slug) return ''
  return getScenarioMeta(slug)?.name || slug
}

function scenarioRoute(slug: string | null | undefined): { path: string; query: { id: string } } | null {
  const meta = getScenarioMeta(slug)
  const pid = effectiveProjectId.value
  if (!meta || !pid) return null
  return {
    path: `/p/${encodeURIComponent(pid)}/scenarios`,
    query: { id: meta.id },
  }
}

function priorityBadgeClass(priority: ScenarioPriority | null): string {
  switch (priority) {
    case 'P0':
      return 'bg-rose-100 text-rose-800 border-rose-200'
    case 'P1':
      return 'bg-amber-100 text-amber-800 border-amber-200'
    case 'P2':
      return 'bg-sky-100 text-sky-800 border-sky-200'
    case 'P3':
      return 'bg-slate-100 text-slate-600 border-slate-200'
    default:
      return 'bg-slate-100 text-slate-600 border-slate-200'
  }
}

const scenarioFilterOptions = computed<ScenarioFilterOption[]>(() => {
  const corr = correlation.value
  if (!corr) return []

  const counts = new Map<string, number>()

  for (const variable of corr.variables) {
    const touched = new Set<string>()
    if (variable.producer?.scenario) touched.add(variable.producer.scenario)
    for (const c of variable.consumers) {
      if (c.scenario) touched.add(c.scenario)
    }
    for (const slug of touched) {
      counts.set(slug, (counts.get(slug) ?? 0) + 1)
    }
  }

  for (const chain of corr.chains) {
    if (chain.scenario && !counts.has(chain.scenario)) {
      counts.set(chain.scenario, 0)
    }
    for (const step of chain.steps) {
      if (step.scenario && !counts.has(step.scenario)) {
        counts.set(step.scenario, 0)
      }
    }
  }

  const priorityRank: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 }

  return Array.from(counts.entries())
    .map(([slug, linkCount]) => {
      const meta = getScenarioMeta(slug)
      return {
        slug,
        label: meta?.name || slug,
        priority: meta?.priority ?? null,
        scenarioId: meta?.id ?? null,
        linkCount,
      }
    })
    .sort((a, b) => {
      if (b.linkCount !== a.linkCount) return b.linkCount - a.linkCount
      const pa = a.priority ? (priorityRank[a.priority] ?? 9) : 9
      const pb = b.priority ? (priorityRank[b.priority] ?? 9) : 9
      if (pa !== pb) return pa - pb
      return a.label.localeCompare(b.label)
    })
})

const filteredVariables = computed<CorrelationVariable[]>(() => {
  const vars = correlation.value?.variables ?? []
  if (selectedScenario.value === 'ALL') return vars
  return vars.filter(
    (v) =>
      v.producer?.scenario === selectedScenario.value ||
      v.consumers.some((c) => c.scenario === selectedScenario.value),
  )
})

interface IndexedChain {
  chain: CorrelationChain
  chainIndex: number
  key: string
}

const filteredChains = computed<IndexedChain[]>(() => {
  const chains = correlation.value?.chains ?? []
  const indexed = chains.map((chain, chainIndex) => ({
    chain,
    chainIndex,
    key: `${chainIndex}::${chain.name}`,
  }))
  if (selectedScenario.value === 'ALL') return indexed
  return indexed.filter(
    ({ chain }) =>
      chain.scenario === selectedScenario.value ||
      chain.steps.some((s) => s.scenario === selectedScenario.value),
  )
})

const activeMermaidCode = computed(() => {
  if (!correlation.value) return ''
  if (selectedScenario.value === 'ALL') {
    return correlation.value.mermaid || buildFocusedMermaid(correlation.value.variables)
  }
  return buildFocusedMermaid(filteredVariables.value)
})

function resetRunnerState() {
  chainRuns.value = []
  credentialByChain.value = {}
  baseUrlByChain.value = {}
  dbDsnRefByChain.value = {}
  initialVarsByChain.value = {}
  activeChainRunByChain.value = {}
  resumeFromIndexByChain.value = {}
  overrideVarsByChain.value = {}
  overrideBodyByChain.value = {}
  chainErrorByChain.value = {}
  expandedRunners.value = {}
  expandedStepPreviews.value = {}
  runningByChain.value = {}
  resumingByChain.value = {}
  syncScenariosByChain.value = {}
}

function isSyncScenarios(chainKey: string): boolean {
  return syncScenariosByChain.value[chainKey] ?? true
}

function setSyncScenarios(chainKey: string, enabled: boolean): void {
  syncScenariosByChain.value = {
    ...syncScenariosByChain.value,
    [chainKey]: enabled,
  }
}

function selectChainCheckpoint(chainKey: string, run: ChainRunOut) {
  activeChainRunByChain.value = {
    ...activeChainRunByChain.value,
    [chainKey]: run,
  }
  resumeFromIndexByChain.value = {
    ...resumeFromIndexByChain.value,
    [chainKey]: computeResumeStepIndex(run),
  }
  overrideVarsByChain.value = {
    ...overrideVarsByChain.value,
    [chainKey]: '',
  }
  overrideBodyByChain.value = {
    ...overrideBodyByChain.value,
    [chainKey]: '',
  }
  chainErrorByChain.value = {
    ...chainErrorByChain.value,
    [chainKey]: '',
  }
  if (
    run.credential_id &&
    authProfiles.value.some((p) => p.id === run.credential_id)
  ) {
    credentialByChain.value = {
      ...credentialByChain.value,
      [chainKey]: run.credential_id,
    }
  } else {
    // Clear stale credential selection when checkpoint had no credential or its credential was deleted
    // so resume falls back to backend checkpoint validation rather than submitting a stale profile.
    credentialByChain.value = {
      ...credentialByChain.value,
      [chainKey]: '',
    }
  }
  if (typeof run.db_dsn_ref === 'string' && run.db_dsn_ref.trim()) {
    dbDsnRefByChain.value = {
      ...dbDsnRefByChain.value,
      [chainKey]: run.db_dsn_ref.trim(),
    }
  } else if (run.db_dsn_ref === null) {
    dbDsnRefByChain.value = {
      ...dbDsnRefByChain.value,
      [chainKey]: '',
    }
  }
}

function hydrateLatestCheckpoints() {
  const chains = correlation.value?.chains ?? []
  for (let i = 0; i < chains.length; i += 1) {
    const chain = chains[i]
    const key = `${i}::${chain.name}`
    if (activeChainRunByChain.value[key]) continue
    const match = chainRuns.value.find((cr) =>
      matchesChainCheckpoint(cr, i, chain.name),
    )
    if (match) {
      selectChainCheckpoint(key, match)
    }
  }
}

function checkpointsForChain(chainIndex: number, chainName: string): ChainRunOut[] {
  return chainRuns.value.filter((cr) =>
    matchesChainCheckpoint(cr, chainIndex, chainName),
  )
}

function toggleChainRunner(chainKey: string) {
  const next = !expandedRunners.value[chainKey]
  expandedRunners.value = {
    ...expandedRunners.value,
    [chainKey]: next,
  }
}

function setResumeStep(chainKey: string, stepIndex: number) {
  resumeFromIndexByChain.value = {
    ...resumeFromIndexByChain.value,
    [chainKey]: Math.max(0, stepIndex),
  }
}

function stepPreviewKey(chainRunId: string, stepIndex: number): string {
  return `${chainRunId}::${stepIndex}`
}

function toggleStepPreview(chainRunId: string, stepIndex: number) {
  const k = stepPreviewKey(chainRunId, stepIndex)
  expandedStepPreviews.value = {
    ...expandedStepPreviews.value,
    [k]: !expandedStepPreviews.value[k],
  }
}

function parseJsonObjectOrError(
  raw: string | undefined,
  fieldLabel: string,
): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  const trimmed = (raw ?? '').trim()
  if (!trimmed) return { ok: true, value: {} }
  try {
    const parsed = JSON.parse(trimmed)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return { ok: true, value: parsed as Record<string, unknown> }
    }
    return { ok: false, error: t('corr.runner.invalidJsonError', { field: fieldLabel }) }
  } catch {
    return { ok: false, error: t('corr.runner.invalidJsonError', { field: fieldLabel }) }
  }
}

function upsertChainRunInHistory(updated: ChainRunOut) {
  const idx = chainRuns.value.findIndex((r) => r.chain_run_id === updated.chain_run_id)
  if (idx >= 0) {
    const copy = [...chainRuns.value]
    copy[idx] = updated
    chainRuns.value = copy
  } else {
    chainRuns.value = [updated, ...chainRuns.value]
  }
}

async function handleExecuteChain(item: IndexedChain) {
  const pid = effectiveProjectId.value
  if (!pid) {
    chainErrorByChain.value = {
      ...chainErrorByChain.value,
      [item.key]: t('corr.runner.noProjectError'),
    }
    return
  }

  const varsRes = parseJsonObjectOrError(
    initialVarsByChain.value[item.key],
    t('corr.runner.initialVarsLabel'),
  )
  if (!varsRes.ok) {
    chainErrorByChain.value = {
      ...chainErrorByChain.value,
      [item.key]: varsRes.error,
    }
    return
  }

  const reqSeq = loadSeq
  const reqRunnerSeq = runnerSeq
  runningByChain.value = { ...runningByChain.value, [item.key]: true }
  chainErrorByChain.value = { ...chainErrorByChain.value, [item.key]: '' }

  try {
    const result = await executeChain(pid, {
      run_id: props.runId,
      chain_name: item.chain.name,
      chain_index: item.chainIndex,
      credential_id: credentialByChain.value[item.key] || null,
      base_url: (baseUrlByChain.value[item.key] ?? '').trim() || null,
      db_dsn_ref: (dbDsnRefByChain.value[item.key] ?? '').trim() || null,
      initial_vars: varsRes.value,
      sync_scenarios: isSyncScenarios(item.key),
      stop_on_failure: true,
    })
    if (reqSeq !== loadSeq || reqRunnerSeq !== runnerSeq) return
    upsertChainRunInHistory(result)
    selectChainCheckpoint(item.key, result)
    if (hasSyncFeedback(result)) {
      emit('synced', {
        synced_scenarios: result.synced_scenarios ?? 0,
        synced_assertions: result.synced_assertions ?? 0,
      })
    }
  } catch (err) {
    if (reqSeq !== loadSeq || reqRunnerSeq !== runnerSeq) return
    chainErrorByChain.value = {
      ...chainErrorByChain.value,
      [item.key]: err instanceof Error ? err.message : String(err),
    }
  } finally {
    if (reqSeq === loadSeq && reqRunnerSeq === runnerSeq) {
      runningByChain.value = { ...runningByChain.value, [item.key]: false }
    }
  }
}

async function handleResumeChain(item: IndexedChain) {
  const pid = effectiveProjectId.value
  const activeRun = activeChainRunByChain.value[item.key]
  if (!pid || !activeRun) {
    chainErrorByChain.value = {
      ...chainErrorByChain.value,
      [item.key]: t('corr.runner.noProjectError'),
    }
    return
  }

  const varsRes = parseJsonObjectOrError(
    overrideVarsByChain.value[item.key],
    t('corr.runner.overrideVarsLabel'),
  )
  if (!varsRes.ok) {
    chainErrorByChain.value = {
      ...chainErrorByChain.value,
      [item.key]: varsRes.error,
    }
    return
  }

  let parsedOverrideBody: unknown = undefined
  const rawBody = (overrideBodyByChain.value[item.key] ?? '').trim()
  if (rawBody) {
    try {
      parsedOverrideBody = JSON.parse(rawBody)
    } catch {
      chainErrorByChain.value = {
        ...chainErrorByChain.value,
        [item.key]: t('corr.runner.invalidJsonError', {
          field: t('corr.runner.overrideBodyLabel'),
        }),
      }
      return
    }
  }

  const fromStepIdx =
    resumeFromIndexByChain.value[item.key] ?? computeResumeStepIndex(activeRun)

  const reqSeq = loadSeq
  const reqRunnerSeq = runnerSeq
  resumingByChain.value = { ...resumingByChain.value, [item.key]: true }
  chainErrorByChain.value = { ...chainErrorByChain.value, [item.key]: '' }

  try {
    const rawOverrideVarsText = (overrideVarsByChain.value[item.key] ?? '').trim()
    const selectedCred = (credentialByChain.value[item.key] ?? '').trim()
    const result = await resumeChainRun(pid, activeRun.chain_run_id, {
      from_step_index: fromStepIdx,
      ...(rawOverrideVarsText ? { override_vars: varsRes.value } : {}),
      ...(parsedOverrideBody !== undefined ? { override_step_body: parsedOverrideBody } : {}),
      ...(selectedCred ? { credential_id: selectedCred } : {}),
      base_url: (baseUrlByChain.value[item.key] ?? '').trim() || null,
      db_dsn_ref: (dbDsnRefByChain.value[item.key] ?? '').trim() || null,
      sync_scenarios: isSyncScenarios(item.key),
      stop_on_failure: true,
    })
    if (reqSeq !== loadSeq || reqRunnerSeq !== runnerSeq) return
    upsertChainRunInHistory(result)
    selectChainCheckpoint(item.key, result)
    if (hasSyncFeedback(result)) {
      emit('synced', {
        synced_scenarios: result.synced_scenarios ?? 0,
        synced_assertions: result.synced_assertions ?? 0,
      })
    }
  } catch (err) {
    if (reqSeq !== loadSeq || reqRunnerSeq !== runnerSeq) return
    chainErrorByChain.value = {
      ...chainErrorByChain.value,
      [item.key]: err instanceof Error ? err.message : String(err),
    }
  } finally {
    if (reqSeq === loadSeq && reqRunnerSeq === runnerSeq) {
      resumingByChain.value = { ...resumingByChain.value, [item.key]: false }
    }
  }
}

function stepStatusBadgeClass(status: ChainStepResult['status']): string {
  switch (status) {
    case 'passed':
      return 'border-emerald-200 bg-emerald-50 text-emerald-700'
    case 'failed':
      return 'border-rose-200 bg-rose-50 text-rose-700'
    default:
      return 'border-slate-200 bg-slate-100 text-slate-500'
  }
}

function formatVarChipValue(val: unknown): string {
  if (typeof val === 'string') {
    return val.length > 28 ? `${val.slice(0, 25)}...` : val
  }
  const serialized = JSON.stringify(val)
  if (!serialized) return String(val)
  return serialized.length > 28 ? `${serialized.slice(0, 25)}...` : serialized
}

async function loadRunnerResources() {
  const seq = ++runnerSeq
  const pid = effectiveProjectId.value
  const currentRunId = props.runId
  if (!pid) {
    authProfiles.value = []
    chainRuns.value = []
    return
  }
  const [authRes, runsRes] = await Promise.allSettled([
    listAuthProfiles(pid),
    listChainRuns(pid, currentRunId),
  ])
  if (seq !== runnerSeq) return
  if (authRes.status === 'fulfilled') {
    authProfiles.value = authRes.value ?? []
  } else {
    authProfiles.value = []
  }
  if (runsRes.status === 'fulfilled') {
    chainRuns.value = runsRes.value ?? []
  } else {
    chainRuns.value = []
  }
  hydrateLatestCheckpoints()
}

async function loadScenarios() {
  const pid = effectiveProjectId.value
  if (!pid) {
    scenarioMap.value = {}
    return
  }
  try {
    const res = await listProjectScenarios(pid)
    if (pid !== effectiveProjectId.value) return
    const nextMap: Record<string, ScenarioMeta> = {}
    for (const sc of (res.scenarios ?? []) as ScenarioItem[]) {
      nextMap[sc.slug] = {
        id: sc.id,
        slug: sc.slug,
        name: sc.name,
        priority: sc.priority,
        effectiveness_level: sc.effectiveness_level,
      }
    }
    scenarioMap.value = nextMap
  } catch {
    if (pid === effectiveProjectId.value) {
      scenarioMap.value = {}
    }
  }
}

async function loadData() {
  const seq = ++loadSeq
  loading.value = true
  correlation.value = null
  coverage.value = null
  selectedScenario.value = 'ALL'
  renderedSvg.value = ''
  mermaidError.value = ''
  expandedPrefixes.value = {}
  visibleLimitByPrefix.value = {}
  uncoveredSearch.value = ''
  resetRunnerState()

  const [corrRes, covRes] = await Promise.allSettled([
    getRunCorrelation(props.runId),
    getRunCoverage(props.runId),
    loadScenarios(),
    loadRunnerResources(),
  ])

  if (seq !== loadSeq) return

  if (corrRes.status === 'fulfilled') {
    correlation.value = corrRes.value
  }
  if (covRes.status === 'fulfilled') {
    coverage.value = covRes.value
  }
  hydrateLatestCheckpoints()
  loading.value = false

  if (activeMermaidCode.value) {
    await renderDiagram(activeMermaidCode.value)
  }
}

async function copyMermaidCode() {
  const code = activeMermaidCode.value
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

function extractFirstSegmentPrefix(rawPath: string): string {
  const cleaned = rawPath.split('?')[0]?.trim() || '/'
  const segments = cleaned.split('/').filter(Boolean)
  return segments.length > 0 ? `/${segments[0]}` : '/'
}

function safePrefixDomId(prefix: string): string {
  return `uncovered-group-${prefix.replace(/[^a-zA-Z0-9_-]/g, '_')}`
}

const uncoveredPrefixGroups = computed<UncoveredPrefixGroup[]>(() => {
  const rawList = coverage.value?.uncovered_endpoints ?? []
  const q = uncoveredSearch.value.trim().toLowerCase()

  const filtered = q
    ? rawList.filter((ep) => {
        if (ep.path.toLowerCase().includes(q)) return true
        if (ep.method.toLowerCase().includes(q)) return true
        if (ep.source.toLowerCase().includes(q)) return true
        if (ep.inferred_params.some((p) => p.toLowerCase().includes(q))) return true
        return false
      })
    : rawList

  const groupMap = new Map<string, UncoveredEndpoint[]>()
  for (const ep of filtered) {
    const prefix = extractFirstSegmentPrefix(ep.path)
    const list = groupMap.get(prefix)
    if (list) {
      list.push(ep)
    } else {
      groupMap.set(prefix, [ep])
    }
  }

  return Array.from(groupMap.entries())
    .map(([prefix, endpoints]) => {
      const methodCounts = new Map<string, number>()
      for (const ep of endpoints) {
        const m = (ep.method || 'GET').toUpperCase()
        methodCounts.set(m, (methodCounts.get(m) ?? 0) + 1)
      }
      const methods = Array.from(methodCounts.entries())
        .map(([method, count]) => ({ method, count }))
        .sort((a, b) => b.count - a.count)

      return {
        prefix,
        endpoints,
        methods,
      }
    })
    .sort((a, b) => b.endpoints.length - a.endpoints.length || a.prefix.localeCompare(b.prefix))
})

const totalMatchedUncovered = computed(() =>
  uncoveredPrefixGroups.value.reduce((acc, g) => acc + g.endpoints.length, 0),
)

function isPrefixExpanded(prefix: string): boolean {
  if (uncoveredSearch.value.trim().length > 0) {
    return expandedPrefixes.value[prefix] !== false
  }
  return Boolean(expandedPrefixes.value[prefix])
}

function togglePrefixGroup(prefix: string) {
  const currentlyOpen = isPrefixExpanded(prefix)
  expandedPrefixes.value = {
    ...expandedPrefixes.value,
    [prefix]: !currentlyOpen,
  }
}

function getVisibleLimit(prefix: string): number {
  return visibleLimitByPrefix.value[prefix] ?? INITIAL_GROUP_LIMIT
}

function showMoreInGroup(prefix: string) {
  visibleLimitByPrefix.value = {
    ...visibleLimitByPrefix.value,
    [prefix]: getVisibleLimit(prefix) + LOAD_MORE_STEP,
  }
}

async function focusUncoveredPrefix(rawPrefix: string) {
  if (!coverage.value || coverage.value.uncovered_endpoints.length === 0) return
  const normalizedFirst = extractFirstSegmentPrefix(rawPrefix)
  const targetGroup =
    uncoveredPrefixGroups.value.find((g) => g.prefix === rawPrefix) ??
    uncoveredPrefixGroups.value.find((g) => g.prefix === normalizedFirst)
  if (!targetGroup) return

  expandedPrefixes.value = {
    ...expandedPrefixes.value,
    [targetGroup.prefix]: true,
  }
  await nextTick()
  const el = document.getElementById(safePrefixDomId(targetGroup.prefix))
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }
}

function methodPillClass(method: string): string {
  switch (method.toUpperCase()) {
    case 'GET':
      return 'bg-emerald-50 text-emerald-700 border-emerald-200'
    case 'POST':
      return 'bg-indigo-50 text-indigo-700 border-indigo-200'
    case 'PUT':
    case 'PATCH':
      return 'bg-amber-50 text-amber-700 border-amber-200'
    case 'DELETE':
      return 'bg-rose-50 text-rose-700 border-rose-200'
    default:
      return 'bg-slate-100 text-slate-700 border-slate-200'
  }
}

watch(activeMermaidCode, async (code) => {
  if (loading.value) return
  showRawMermaid.value = false
  await renderDiagram(code)
})

watch(() => props.runId, loadData)
watch(effectiveProjectId, () => {
  resetRunnerState()
  void loadScenarios()
  void loadRunnerResources()
})
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
                <template v-if="selectedScenario === 'ALL'">
                  {{ t('corr.graphSubtitle', { vars: correlation.variables.length, chains: correlation.chains.length }) }}
                </template>
                <template v-else>
                  {{
                    t('corr.scenarioFilteredSubtitle', {
                      scenario: scenarioDisplayName(selectedScenario),
                      vars: filteredVariables.length,
                    })
                  }}
                </template>
              </p>
            </div>
          </div>

          <div v-if="activeMermaidCode" class="flex items-center gap-2">
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

        <!-- Business Scenario Filter Bar -->
        <div
          v-if="scenarioFilterOptions.length > 0"
          data-testid="corr-scenario-filter"
          class="flex flex-wrap items-center gap-2 border-b border-slate-100 bg-slate-50/70 px-5 py-3"
        >
          <span class="text-[11px] font-semibold tracking-wide text-slate-500 uppercase">
            {{ t('corr.scenarioFilterLabel') }}
          </span>

          <button
            type="button"
            class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition"
            :class="
              selectedScenario === 'ALL'
                ? 'border-indigo-600 bg-indigo-600 text-white shadow-xs'
                : 'border-slate-200 bg-white text-slate-700 hover:border-indigo-300 hover:bg-indigo-50/60'
            "
            @click="selectedScenario = 'ALL'"
          >
            <span>{{ t('corr.allScenarios') }}</span>
            <span
              class="rounded-full px-1.5 py-0.2 font-mono text-[10px]"
              :class="selectedScenario === 'ALL' ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-600'"
            >
              {{ correlation.variables.length }}
            </span>
          </button>

          <button
            v-for="opt in scenarioFilterOptions"
            :key="opt.slug"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition"
            :class="
              selectedScenario === opt.slug
                ? 'border-indigo-600 bg-indigo-600 text-white shadow-xs'
                : 'border-slate-200 bg-white text-slate-700 hover:border-indigo-300 hover:bg-indigo-50/60'
            "
            @click="selectedScenario = opt.slug"
          >
            <span
              v-if="opt.priority"
              class="rounded px-1 py-0.2 text-[10px] font-bold"
              :class="
                selectedScenario === opt.slug
                  ? 'bg-white/20 text-white'
                  : priorityBadgeClass(opt.priority)
              "
            >
              {{ opt.priority }}
            </span>
            <span class="max-w-[180px] truncate">{{ opt.label }}</span>
            <span
              class="rounded-full px-1.5 py-0.2 font-mono text-[10px]"
              :class="selectedScenario === opt.slug ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-600'"
            >
              {{ opt.linkCount }}
            </span>
          </button>
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
            v-if="!activeMermaidCode"
            class="rounded-lg border border-dashed border-slate-200 bg-slate-50 py-8 text-center text-xs text-slate-500"
          >
            {{ selectedScenario === 'ALL' ? t('corr.noGraphEdges') : t('corr.noGraphForScenario') }}
          </div>

          <pre
            v-else-if="showRawMermaid || !renderedSvg"
            class="max-h-96 overflow-auto rounded-lg bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-100"
          >{{ activeMermaidCode }}</pre>

          <div
            v-else
            class="mermaid-container overflow-x-auto rounded-lg border border-slate-100 bg-slate-50/50 p-4 [&>svg]:mx-auto [&>svg]:h-auto [&>svg]:max-w-full"
            v-html="renderedSvg"
          />

          <!-- Chains Summary Strip & Lightweight Chain Runner -->
          <div
            v-if="filteredChains.length > 0"
            class="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2"
          >
            <div
              v-for="item in filteredChains"
              :key="item.key"
              class="rounded-xl border border-slate-200 bg-slate-50/80 text-xs transition"
              :class="expandedRunners[item.key] ? 'md:col-span-2 bg-white shadow-sm ring-1 ring-indigo-100' : ''"
            >
              <!-- Chain Card Header -->
              <div class="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2.5">
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="truncate font-semibold text-slate-800">{{ item.chain.name }}</span>
                    <span
                      v-if="activeChainRunByChain[item.key]"
                      class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold"
                      :class="
                        activeChainRunByChain[item.key]!.status === 'passed'
                          ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                          : 'border-rose-200 bg-rose-50 text-rose-700'
                      "
                    >
                      <CheckCircle2
                        v-if="activeChainRunByChain[item.key]!.status === 'passed'"
                        class="h-3 w-3"
                      />
                      <XCircle v-else class="h-3 w-3" />
                      <span>
                        {{ activeChainRunByChain[item.key]!.completed_steps }}/{{
                          activeChainRunByChain[item.key]!.total_steps
                        }}
                      </span>
                    </span>
                  </div>
                  <div class="mt-0.5 flex flex-wrap items-center gap-1.5 text-slate-500">
                    <RouterLink
                      v-if="scenarioRoute(item.chain.scenario)"
                      :to="scenarioRoute(item.chain.scenario)!"
                      class="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700 transition hover:bg-indigo-100"
                    >
                      <span
                        v-if="getScenarioMeta(item.chain.scenario)?.priority"
                        class="rounded px-1 text-[9px] font-bold"
                        :class="priorityBadgeClass(getScenarioMeta(item.chain.scenario)!.priority)"
                      >
                        {{ getScenarioMeta(item.chain.scenario)!.priority }}
                      </span>
                      <span class="max-w-[160px] truncate">{{
                        scenarioDisplayName(item.chain.scenario)
                      }}</span>
                    </RouterLink>
                    <span>{{
                      t('corr.chainMeta', {
                        scenario: scenarioDisplayName(item.chain.scenario),
                        steps: item.chain.steps.length,
                      })
                    }}</span>
                  </div>
                </div>

                <div class="flex shrink-0 flex-wrap items-center gap-2">
                  <span
                    v-if="item.chain.cleanup_step"
                    class="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700"
                  >
                    {{
                      t('corr.cleanupBadge', {
                        method: item.chain.cleanup_step.method,
                        path: item.chain.cleanup_step.path,
                      })
                    }}
                  </span>
                  <button
                    type="button"
                    data-testid="chain-runner-toggle-btn"
                    class="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] font-semibold transition"
                    :class="
                      expandedRunners[item.key]
                        ? 'border-indigo-300 bg-indigo-600 text-white hover:bg-indigo-700'
                        : 'border-indigo-200 bg-white text-indigo-700 hover:bg-indigo-50'
                    "
                    @click="toggleChainRunner(item.key)"
                  >
                    <Play v-if="!expandedRunners[item.key]" class="h-3 w-3" />
                    <ChevronDown v-else class="h-3 w-3" />
                    <span>
                      {{
                        expandedRunners[item.key]
                          ? t('corr.runner.toggleClose')
                          : t('corr.runner.toggleOpen')
                      }}
                    </span>
                  </button>
                </div>
              </div>

              <!-- Collapsible Chain Runner & Checkpoint/Resume Drawer -->
              <div
                v-if="expandedRunners[item.key]"
                class="space-y-4 border-t border-slate-200 bg-white px-4 py-4"
              >
                <!-- Recent Checkpoints Selector Pill Bar -->
                <div
                  v-if="checkpointsForChain(item.chainIndex, item.chain.name).length > 0"
                  class="flex flex-wrap items-center gap-1.5"
                >
                  <span class="inline-flex items-center gap-1 text-[11px] font-semibold text-slate-500">
                    <History class="h-3.5 w-3.5" />
                    {{ t('corr.runner.recentCheckpoints') }}:
                  </span>
                  <button
                    v-for="cp in checkpointsForChain(item.chainIndex, item.chain.name)"
                    :key="cp.chain_run_id"
                    type="button"
                    class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[11px] transition"
                    :class="
                      activeChainRunByChain[item.key]?.chain_run_id === cp.chain_run_id
                        ? 'border-indigo-400 bg-indigo-50 font-semibold text-indigo-800'
                        : 'border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100'
                    "
                    @click="selectChainCheckpoint(item.key, cp)"
                  >
                    <span
                      class="h-1.5 w-1.5 rounded-full"
                      :class="cp.status === 'passed' ? 'bg-emerald-500' : 'bg-rose-500'"
                    />
                    <span>#{{ cp.chain_run_id.slice(0, 8) }}</span>
                    <span class="text-slate-400">{{ cp.completed_steps }}/{{ cp.total_steps }}</span>
                  </button>
                </div>

                <!-- Execution Controls -->
                <div class="grid grid-cols-1 gap-3 rounded-lg border border-slate-200 bg-slate-50/70 p-3 md:grid-cols-3">
                  <div>
                    <label class="mb-1 block text-[11px] font-medium text-slate-600">
                      {{ t('corr.runner.credentialLabel') }}
                    </label>
                    <select
                      v-model="credentialByChain[item.key]"
                      class="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-800 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100"
                    >
                      <option value="">{{ t('corr.runner.credentialNone') }}</option>
                      <option
                        v-for="prof in authProfiles"
                        :key="prof.id"
                        :value="prof.id"
                      >
                        {{ prof.name }} ({{ prof.role_tag }})
                      </option>
                    </select>
                  </div>

                  <div>
                    <label class="mb-1 block text-[11px] font-medium text-slate-600">
                      {{ t('corr.runner.baseUrlLabel') }}
                    </label>
                    <input
                      v-model="baseUrlByChain[item.key]"
                      type="text"
                      :placeholder="t('corr.runner.baseUrlPlaceholder')"
                      class="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 font-mono text-xs text-slate-800 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100"
                    />
                  </div>

                  <div>
                    <label class="mb-1 flex items-center gap-1 text-[11px] font-medium text-slate-600">
                      <Database class="h-3 w-3 text-violet-500" />
                      <span>{{ t('corr.runner.dbDsnLabel') }}</span>
                    </label>
                    <input
                      v-model="dbDsnRefByChain[item.key]"
                      type="text"
                      :placeholder="t('corr.runner.dbDsnPlaceholder')"
                      class="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 font-mono text-xs text-slate-800 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100"
                    />
                  </div>

                  <div class="md:col-span-2">
                    <label class="mb-1 block text-[11px] font-medium text-slate-600">
                      {{ t('corr.runner.initialVarsLabel') }}
                    </label>
                    <input
                      v-model="initialVarsByChain[item.key]"
                      type="text"
                      :placeholder="t('corr.runner.initialVarsPlaceholder')"
                      class="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 font-mono text-xs text-slate-800 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100"
                    />
                  </div>

                  <div class="flex flex-wrap items-end justify-between gap-2">
                    <label class="inline-flex cursor-pointer items-center gap-1.5 pb-1 text-[11px] font-medium text-slate-700">
                      <input
                        type="checkbox"
                        data-testid="chain-sync-scenarios-toggle"
                        :checked="isSyncScenarios(item.key)"
                        :disabled="runningByChain[item.key] || resumingByChain[item.key]"
                        class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                        @change="setSyncScenarios(item.key, ($event.target as HTMLInputElement).checked)"
                      />
                      <span>{{ t('corr.runner.syncScenariosToggle') }}</span>
                    </label>
                    <button
                      type="button"
                      data-testid="chain-run-btn"
                      :disabled="runningByChain[item.key] || resumingByChain[item.key]"
                      class="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-1.5 text-xs font-semibold text-white shadow-sm transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60 md:w-auto"
                      @click="handleExecuteChain(item)"
                    >
                      <Loader2 v-if="runningByChain[item.key]" class="h-3.5 w-3.5 animate-spin" />
                      <Play v-else class="h-3.5 w-3.5" />
                      <span>
                        {{
                          runningByChain[item.key]
                            ? t('corr.runner.runningBtn')
                            : t('corr.runner.runBtn')
                        }}
                      </span>
                    </button>
                  </div>
                </div>

                <!-- Error Banner -->
                <div
                  v-if="chainErrorByChain[item.key]"
                  class="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800"
                >
                  <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0 text-rose-600" />
                  <span class="break-all">{{ chainErrorByChain[item.key] }}</span>
                </div>

                <!-- Live Step Timeline & Checkpoint Resume -->
                <div
                  v-if="activeChainRunByChain[item.key]"
                  class="space-y-3"
                >
                  <!-- Overall Summary Header -->
                  <div
                    class="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3.5 py-2.5"
                    :class="
                      activeChainRunByChain[item.key]!.status === 'passed'
                        ? 'border-emerald-200 bg-emerald-50/70'
                        : 'border-rose-200 bg-rose-50/70'
                    "
                  >
                    <div class="flex flex-wrap items-center gap-2">
                      <span
                        class="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-bold"
                        :class="
                          activeChainRunByChain[item.key]!.status === 'passed'
                            ? 'bg-emerald-600 text-white'
                            : 'bg-rose-600 text-white'
                        "
                      >
                        <CheckCircle2
                          v-if="activeChainRunByChain[item.key]!.status === 'passed'"
                          class="h-3.5 w-3.5"
                        />
                        <XCircle v-else class="h-3.5 w-3.5" />
                        <span>
                          {{
                            activeChainRunByChain[item.key]!.status === 'passed'
                              ? t('corr.runner.statusPassed')
                              : activeChainRunByChain[item.key]!.failed_step_index != null
                                ? t('corr.runner.statusFailedAt', {
                                    step: activeChainRunByChain[item.key]!.failed_step_index! + 1,
                                  })
                                : t('corr.runner.statusFailed')
                          }}
                        </span>
                      </span>

                      <span class="text-xs font-semibold text-slate-700">
                        {{
                          t('corr.runner.progress', {
                            completed: activeChainRunByChain[item.key]!.completed_steps,
                            total: activeChainRunByChain[item.key]!.total_steps,
                          })
                        }}
                      </span>

                      <span
                        v-if="activeChainRunByChain[item.key]!.resumed_from_index != null"
                        class="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700"
                      >
                        <RotateCcw class="h-3 w-3" />
                        <span>
                          {{
                            t('corr.runner.resumedFrom', {
                              step: activeChainRunByChain[item.key]!.resumed_from_index! + 1,
                            })
                          }}
                        </span>
                      </span>
                    </div>

                    <span
                      v-if="activeChainRunByChain[item.key]!.base_url"
                      class="font-mono text-[11px] text-slate-500"
                    >
                      {{ activeChainRunByChain[item.key]!.base_url }}
                    </span>
                  </div>

                  <!-- Scenario L3 Sync Feedback Banner -->
                  <div
                    v-if="hasSyncFeedback(activeChainRunByChain[item.key])"
                    data-testid="chain-synced-badge"
                    class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3.5 py-2 text-xs text-emerald-900"
                  >
                    <div class="flex items-center gap-2">
                      <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-600" />
                      <span class="font-medium">
                        {{
                          t('corr.runner.syncedBadge', {
                            scenarios: activeChainRunByChain[item.key]!.synced_scenarios ?? 0,
                            assertions: activeChainRunByChain[item.key]!.synced_assertions ?? 0,
                          })
                        }}
                      </span>
                    </div>
                    <RouterLink
                      v-if="effectiveProjectId"
                      :to="`/p/${encodeURIComponent(effectiveProjectId)}/scenarios`"
                      class="font-semibold text-emerald-800 hover:underline"
                    >
                      {{ t('corr.runner.viewScenarios') }}
                    </RouterLink>
                  </div>

                  <!-- Per-Step Timeline -->
                  <ol class="space-y-2">
                    <li
                      v-for="stepRes in activeChainRunByChain[item.key]!.step_results"
                      :key="`${activeChainRunByChain[item.key]!.chain_run_id}-${stepRes.step_index}`"
                      class="rounded-lg border border-slate-200 bg-white p-3"
                      :class="
                        stepRes.status === 'failed'
                          ? 'border-rose-300 bg-rose-50/30'
                          : stepRes.status === 'skipped'
                            ? 'opacity-70'
                            : ''
                      "
                    >
                      <div class="flex flex-wrap items-center justify-between gap-2">
                        <div class="flex min-w-0 flex-wrap items-center gap-2">
                          <CheckCircle2
                            v-if="stepRes.status === 'passed'"
                            class="h-4 w-4 shrink-0 text-emerald-600"
                          />
                          <XCircle
                            v-else-if="stepRes.status === 'failed'"
                            class="h-4 w-4 shrink-0 text-rose-600"
                          />
                          <SkipForward
                            v-else
                            class="h-4 w-4 shrink-0 text-slate-400"
                          />

                          <span
                            class="rounded-full border px-2 py-0.5 text-[10px] font-semibold"
                            :class="stepStatusBadgeClass(stepRes.status)"
                          >
                            {{ t('corr.runner.stepLabel', { step: stepRes.step_index + 1 }) }} ·
                            {{ t(`corr.runner.stepStatus.${stepRes.status}`) }}
                          </span>

                          <span
                            class="rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold"
                            :class="methodPillClass(stepRes.method)"
                          >
                            {{ stepRes.method }}
                          </span>

                          <span class="truncate font-mono text-xs text-slate-800">
                            {{ stepRes.url || stepRes.name }}
                          </span>
                        </div>

                        <div class="flex shrink-0 items-center gap-2">
                          <span
                            v-if="stepRes.status_code > 0"
                            class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] font-semibold"
                            :class="
                              stepRes.status_code < 400 ? 'text-emerald-700' : 'text-rose-700'
                            "
                          >
                            HTTP {{ stepRes.status_code }}
                          </span>
                          <span class="inline-flex items-center gap-0.5 font-mono text-[11px] text-slate-500">
                            <Clock class="h-3 w-3" />
                            {{ Math.round(stepRes.duration_ms) }} ms
                          </span>
                          <button
                            type="button"
                            class="rounded border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-medium text-slate-600 transition hover:border-indigo-300 hover:bg-indigo-50 hover:text-indigo-700"
                            @click="setResumeStep(item.key, stepRes.step_index)"
                          >
                            {{ t('corr.runner.setResumeStep') }}
                          </button>
                          <button
                            type="button"
                            class="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-medium text-slate-600 hover:bg-slate-50"
                            @click="
                              toggleStepPreview(
                                activeChainRunByChain[item.key]!.chain_run_id,
                                stepRes.step_index,
                              )
                            "
                          >
                            <Terminal class="h-3 w-3" />
                            <span>
                              {{
                                expandedStepPreviews[
                                  stepPreviewKey(
                                    activeChainRunByChain[item.key]!.chain_run_id,
                                    stepRes.step_index,
                                  )
                                ]
                                  ? t('corr.runner.previewHide')
                                  : t('corr.runner.previewShow')
                              }}
                            </span>
                          </button>
                        </div>
                      </div>

                      <!-- Extracted {{var}} Chips -->
                      <div
                        v-if="Object.keys(stepRes.extracted_vars || {}).length > 0"
                        class="mt-2 flex flex-wrap items-center gap-1.5"
                      >
                        <span class="text-[10px] font-semibold text-slate-500">
                          {{ t('corr.runner.extractedVars') }}:
                        </span>
                        <span
                          v-for="[vName, vVal] in Object.entries(stepRes.extracted_vars)"
                          :key="vName"
                          class="inline-flex items-center gap-1 rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 font-mono text-[10px] text-indigo-800"
                        >
                          <span class="font-semibold">&#123;&#123;{{ vName }}&#125;&#125;</span>
                          <span class="text-indigo-500">=</span>
                          <span>{{ formatVarChipValue(vVal) }}</span>
                        </span>
                      </div>

                      <!-- Assertion Pass/Fail Chips -->
                      <div
                        v-if="stepRes.assertions && stepRes.assertions.length > 0"
                        class="mt-2 flex flex-wrap items-center gap-1.5"
                      >
                        <span class="text-[10px] font-semibold text-slate-500">
                          {{ t('corr.runner.assertions') }}:
                        </span>
                        <span
                          v-for="asrt in stepRes.assertions"
                          :key="asrt.id || asrt.title"
                          :title="asrt.message"
                          class="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium"
                          :class="
                            asrt.passed
                              ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                              : 'border-rose-200 bg-rose-50 text-rose-800'
                          "
                        >
                          <CheckCircle2 v-if="asrt.passed" class="h-3 w-3 shrink-0" />
                          <XCircle v-else class="h-3 w-3 shrink-0" />
                          <span>{{ asrt.title || asrt.operator }}</span>
                          <span v-if="!asrt.passed && asrt.message" class="font-mono text-[10px] opacity-90">
                            — {{ asrt.message }}
                          </span>
                        </span>
                      </div>

                      <!-- Step Error Message -->
                      <div
                        v-if="stepRes.error"
                        class="mt-2 rounded border border-rose-200 bg-rose-50 px-2.5 py-1.5 font-mono text-[11px] text-rose-800"
                      >
                        {{ stepRes.error }}
                      </div>

                      <!-- Expandable Response Body Preview -->
                      <pre
                        v-if="
                          expandedStepPreviews[
                            stepPreviewKey(
                              activeChainRunByChain[item.key]!.chain_run_id,
                              stepRes.step_index,
                            )
                          ]
                        "
                        class="mt-2 max-h-48 overflow-auto rounded-lg bg-slate-950 p-2.5 font-mono text-[11px] leading-relaxed text-slate-100"
                      >{{ stepRes.body_preview || t('corr.runner.emptyBody') }}</pre>
                    </li>
                  </ol>

                  <!-- Checkpoint & Resume Controls -->
                  <div
                    class="space-y-3 rounded-xl border p-3.5"
                    :class="
                      activeChainRunByChain[item.key]!.status === 'failed'
                        ? 'border-amber-300 bg-amber-50/60'
                        : 'border-slate-200 bg-slate-50/70'
                    "
                  >
                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <h5 class="flex items-center gap-1.5 text-xs font-bold text-slate-900">
                          <RotateCcw class="h-3.5 w-3.5 text-amber-600" />
                          <span>{{ t('corr.runner.resumeTitle') }}</span>
                        </h5>
                        <p class="mt-0.5 text-[11px] text-slate-600">
                          {{ t('corr.runner.resumeHint') }}
                        </p>
                      </div>

                      <div class="flex items-center gap-2">
                        <label class="text-[11px] font-medium text-slate-700">
                          {{ t('corr.runner.resumeFromLabel') }}:
                        </label>
                        <select
                          v-model.number="resumeFromIndexByChain[item.key]"
                          class="rounded-lg border border-slate-300 bg-white px-2 py-1 font-mono text-xs text-slate-800 focus:border-indigo-400 focus:outline-none"
                        >
                          <option
                            v-for="idx in activeChainRunByChain[item.key]!.total_steps"
                            :key="idx"
                            :value="idx - 1"
                          >
                            {{ t('corr.runner.stepLabel', { step: idx }) }}
                          </option>
                        </select>
                      </div>
                    </div>

                    <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
                      <div>
                        <label class="mb-1 block text-[11px] font-medium text-slate-700">
                          {{ t('corr.runner.overrideVarsLabel') }}
                        </label>
                        <textarea
                          v-model="overrideVarsByChain[item.key]"
                          rows="4"
                          class="w-full rounded-lg border border-slate-200 bg-white p-2 font-mono text-[11px] text-slate-800 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100"
                        />
                      </div>

                      <div>
                        <label class="mb-1 block text-[11px] font-medium text-slate-700">
                          {{ t('corr.runner.overrideBodyLabel') }}
                        </label>
                        <textarea
                          v-model="overrideBodyByChain[item.key]"
                          rows="4"
                          :placeholder="t('corr.runner.overrideBodyPlaceholder')"
                          class="w-full rounded-lg border border-slate-200 bg-white p-2 font-mono text-[11px] text-slate-800 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100"
                        />
                      </div>
                    </div>

                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <label class="inline-flex cursor-pointer items-center gap-1.5 text-[11px] font-medium text-slate-700">
                        <input
                          type="checkbox"
                          data-testid="chain-resume-sync-scenarios-toggle"
                          :checked="isSyncScenarios(item.key)"
                          :disabled="runningByChain[item.key] || resumingByChain[item.key]"
                          class="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                          @change="setSyncScenarios(item.key, ($event.target as HTMLInputElement).checked)"
                        />
                        <span>{{ t('corr.runner.syncScenariosToggle') }}</span>
                      </label>
                      <button
                        type="button"
                        data-testid="chain-resume-btn"
                        :disabled="runningByChain[item.key] || resumingByChain[item.key]"
                        class="inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-3.5 py-1.5 text-xs font-semibold text-white shadow-sm transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-60"
                        @click="handleResumeChain(item)"
                      >
                        <Loader2 v-if="resumingByChain[item.key]" class="h-3.5 w-3.5 animate-spin" />
                        <RotateCcw v-else class="h-3.5 w-3.5" />
                        <span>
                          {{
                            resumingByChain[item.key]
                              ? t('corr.runner.resumingBtn')
                              : t('corr.runner.resumeBtn', {
                                  step:
                                    (resumeFromIndexByChain[item.key] ??
                                      computeResumeStepIndex(activeChainRunByChain[item.key])) + 1,
                                })
                          }}
                        </span>
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- Correlated Variables Table & Filtered Static Constants -->
      <section v-if="correlation" class="rounded-xl border border-slate-200 bg-white shadow-sm">
        <div class="border-b border-slate-200 px-5 py-4">
          <h3 class="text-sm font-semibold text-slate-900">
            {{ t('corr.varsTitle', { count: filteredVariables.length }) }}
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
                v-for="variable in filteredVariables"
                :key="variable.var_name"
                class="hover:bg-slate-50/70"
              >
                <td class="px-5 py-3 align-top">
                  <span class="inline-flex rounded-md border border-indigo-200 bg-indigo-50 px-2 py-0.5 font-mono font-semibold text-indigo-700">
                    &#123;&#123;{{ variable.var_name }}&#125;&#125;
                  </span>
                </td>
                <td class="px-4 py-3 align-top">
                  <div class="flex flex-wrap items-center gap-1.5 font-mono text-slate-800">
                    <span class="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold">
                      #{{ variable.producer.step }}
                    </span>
                    <span class="font-semibold text-emerald-700">{{ variable.producer.method }}</span>
                    <span class="break-all">{{ variable.producer.path }}</span>
                    <RouterLink
                      v-if="scenarioRoute(variable.producer.scenario)"
                      :to="scenarioRoute(variable.producer.scenario)!"
                      data-testid="corr-scenario-link"
                      class="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 font-sans text-[10px] font-medium text-indigo-700 transition hover:bg-indigo-100"
                    >
                      <span
                        v-if="getScenarioMeta(variable.producer.scenario)?.priority"
                        class="rounded px-1 text-[9px] font-bold"
                        :class="priorityBadgeClass(getScenarioMeta(variable.producer.scenario)!.priority)"
                      >
                        {{ getScenarioMeta(variable.producer.scenario)!.priority }}
                      </span>
                      <span class="max-w-[140px] truncate">{{ scenarioDisplayName(variable.producer.scenario) }}</span>
                    </RouterLink>
                    <span
                      v-else-if="variable.producer.scenario"
                      class="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 font-sans text-[10px] font-medium text-slate-600"
                    >
                      {{ variable.producer.scenario }}
                    </span>
                  </div>
                  <div v-if="variable.producer.jsonpath" class="mt-1 font-mono text-[11px] text-indigo-600">
                    {{ variable.producer.jsonpath }}
                  </div>
                </td>
                <td class="px-4 py-3 align-top">
                  <ul class="space-y-1.5">
                    <li
                      v-for="(consumer, idx) in variable.consumers"
                      :key="`${variable.var_name}-${idx}`"
                      class="flex flex-wrap items-center gap-1.5 font-mono text-slate-700"
                    >
                      <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-600">
                        #{{ consumer.step }}
                      </span>
                      <span class="font-semibold text-sky-700">{{ consumer.method }}</span>
                      <span class="break-all">{{ consumer.path }}</span>
                      <span
                        v-if="consumer.location || consumer.param"
                        class="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800"
                      >
                        {{ consumer.location }}:{{ consumer.param }}
                      </span>
                      <RouterLink
                        v-if="scenarioRoute(consumer.scenario)"
                        :to="scenarioRoute(consumer.scenario)!"
                        data-testid="corr-scenario-link"
                        class="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 font-sans text-[10px] font-medium text-indigo-700 transition hover:bg-indigo-100"
                      >
                        <span
                          v-if="getScenarioMeta(consumer.scenario)?.priority"
                          class="rounded px-1 text-[9px] font-bold"
                          :class="priorityBadgeClass(getScenarioMeta(consumer.scenario)!.priority)"
                        >
                          {{ getScenarioMeta(consumer.scenario)!.priority }}
                        </span>
                        <span class="max-w-[140px] truncate">{{ scenarioDisplayName(consumer.scenario) }}</span>
                      </RouterLink>
                      <span
                        v-else-if="consumer.scenario"
                        class="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 font-sans text-[10px] font-medium text-slate-600"
                      >
                        {{ consumer.scenario }}
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

              <tr v-if="filteredVariables.length === 0">
                <td colspan="4" class="px-5 py-8 text-center text-slate-500">
                  {{ selectedScenario === 'ALL' ? t('corr.noVars') : t('corr.noVarsForScenario') }}
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
            <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-5 py-3.5">
              <h3 class="text-sm font-semibold text-slate-900">{{ t('corr.prefixTitle') }}</h3>
              <span class="text-[11px] text-slate-400">{{ t('corr.prefixClickHint') }}</span>
            </div>
            <div class="max-h-72 overflow-auto">
              <table class="min-w-full divide-y divide-slate-200 text-left text-xs">
                <thead class="sticky top-0 bg-slate-50 font-semibold text-slate-500 uppercase">
                  <tr>
                    <th class="px-4 py-2.5">{{ t('corr.col.prefix') }}</th>
                    <th class="px-3 py-2.5 text-right">{{ t('corr.col.total') }}</th>
                    <th class="px-3 py-2.5 text-right">{{ t('corr.col.covered') }}</th>
                    <th class="px-3 py-2.5 text-right">{{ t('corr.col.uncovered') }}</th>
                    <th class="w-44 px-4 py-2.5">{{ t('corr.col.coveragePct') }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-slate-100">
                  <tr
                    v-for="row in coverage.by_prefix"
                    :key="row.prefix"
                    :class="
                      row.uncovered > 0
                        ? 'cursor-pointer transition hover:bg-indigo-50/50'
                        : 'hover:bg-slate-50/60'
                    "
                    @click="row.uncovered > 0 && focusUncoveredPrefix(row.prefix)"
                  >
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

        <!-- Collapsed & Aggregated Uncovered Blind-Spot Accordion -->
        <div class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-3.5">
            <div>
              <div class="flex items-center gap-2">
                <ShieldAlert class="h-4 w-4 text-amber-600" />
                <h3 class="text-sm font-semibold text-slate-900">
                  {{ t('corr.blindSpotsTitle', { count: totalMatchedUncovered }) }}
                </h3>
                <span
                  v-if="uncoveredPrefixGroups.length > 0"
                  class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600"
                >
                  {{ t('corr.uncoveredGroupsSummary', { groups: uncoveredPrefixGroups.length }) }}
                </span>
              </div>
              <p class="mt-0.5 text-xs text-slate-500">
                {{ t('corr.blindSpotsHint') }}
              </p>
            </div>

            <div
              v-if="coverage.uncovered_endpoints.length > 0"
              class="relative w-full sm:w-72"
            >
              <Search class="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
              <input
                v-model="uncoveredSearch"
                type="text"
                data-testid="uncovered-search-input"
                :placeholder="t('corr.uncoveredSearchPlaceholder')"
                class="w-full rounded-lg border border-slate-300 bg-white py-1.5 pr-3 pl-8 text-xs text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 focus:outline-none"
              />
            </div>
          </div>

          <div
            v-if="coverage.uncovered_endpoints.length === 0"
            class="px-5 py-8 text-center text-xs text-emerald-700"
          >
            {{ t('corr.allCovered') }}
          </div>

          <div
            v-else-if="uncoveredPrefixGroups.length === 0"
            class="px-5 py-8 text-center text-xs text-slate-500"
          >
            {{ t('corr.uncoveredNoMatch') }}
          </div>

          <div v-else class="divide-y divide-slate-200">
            <div
              v-for="group in uncoveredPrefixGroups"
              :id="safePrefixDomId(group.prefix)"
              :key="group.prefix"
              data-testid="uncovered-prefix-group"
              class="bg-white"
            >
              <button
                type="button"
                class="flex w-full flex-wrap items-center justify-between gap-2 px-5 py-3 text-left transition hover:bg-slate-50"
                @click="togglePrefixGroup(group.prefix)"
              >
                <div class="flex items-center gap-2">
                  <ChevronDown
                    v-if="isPrefixExpanded(group.prefix)"
                    class="h-4 w-4 shrink-0 text-slate-500"
                  />
                  <ChevronRight v-else class="h-4 w-4 shrink-0 text-slate-400" />
                  <span class="font-mono text-xs font-semibold text-slate-900">
                    {{ group.prefix }}
                  </span>
                  <span class="rounded-full bg-rose-50 px-2 py-0.5 text-[11px] font-semibold text-rose-700">
                    {{ t('corr.uncoveredGroupCount', { count: group.endpoints.length }) }}
                  </span>
                </div>

                <div class="flex flex-wrap items-center gap-1.5">
                  <span
                    v-for="m in group.methods"
                    :key="`${group.prefix}-${m.method}`"
                    class="rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold"
                    :class="methodPillClass(m.method)"
                  >
                    {{ m.method }} {{ m.count }}
                  </span>
                </div>
              </button>

              <div
                v-if="isPrefixExpanded(group.prefix)"
                class="border-t border-slate-100 bg-slate-50/40"
              >
                <div class="divide-y divide-slate-100">
                  <div
                    v-for="ep in group.endpoints.slice(0, getVisibleLimit(group.prefix))"
                    :key="`${ep.method}-${ep.path}`"
                    class="flex flex-col gap-1.5 px-6 py-2.5 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div class="min-w-0 space-y-1">
                      <div class="flex flex-wrap items-center gap-2">
                        <StatusBadge :status="ep.priority" size="sm" />
                        <StatusBadge :status="ep.source" size="sm" />
                        <span class="font-mono text-xs font-bold text-slate-700">{{ ep.method }}</span>
                        <span class="break-all font-mono text-xs font-medium text-slate-900">{{ ep.path }}</span>
                      </div>
                      <div
                        v-if="ep.inferred_params.length > 0"
                        class="flex flex-wrap items-center gap-1 text-[11px] text-slate-500"
                      >
                        <span>{{ t('corr.inferredParams') }}</span>
                        <span
                          v-for="param in ep.inferred_params"
                          :key="param"
                          class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-700"
                        >
                          {{ param }}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>

                <div
                  v-if="group.endpoints.length > getVisibleLimit(group.prefix)"
                  class="flex items-center justify-between border-t border-slate-100 bg-white px-6 py-2.5 text-xs"
                >
                  <span class="text-slate-500">
                    {{
                      t('corr.showingCount', {
                        shown: Math.min(getVisibleLimit(group.prefix), group.endpoints.length),
                        total: group.endpoints.length,
                      })
                    }}
                  </span>
                  <button
                    type="button"
                    data-testid="uncovered-show-more-btn"
                    class="rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-1 text-xs font-medium text-indigo-700 transition hover:bg-indigo-100"
                    @click="showMoreInGroup(group.prefix)"
                  >
                    {{
                      t('corr.showMore', {
                        remaining: group.endpoints.length - getVisibleLimit(group.prefix),
                      })
                    }}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>
