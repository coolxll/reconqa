<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Download,
  FileCode2,
  Layers,
  List,
  Lock,
  Search,
} from 'lucide-vue-next'
import { artifactDownloadUrl } from '@/api/client'
import { getRunOpenApi } from '@/api/runs'
import StatusBadge from '@/components/layout/StatusBadge.vue'
import EmptyState from '@/components/layout/EmptyState.vue'
import '@/i18n/locales/runs'
import { useI18n } from '@/i18n'

const { t } = useI18n()

interface OpenApiParameter {
  name: string
  in: string
  required?: boolean
  schema?: Record<string, unknown>
  example?: unknown
}

interface PerfBaseline {
  count?: number
  p50_ms?: number
  p95_ms?: number
  avg_bytes?: number
}

interface BizSuccessRule {
  field?: string
  value?: unknown
}

interface FlattenedEndpoint {
  key: string
  method: string
  path: string
  operationId: string
  summary: string
  tags: string[]
  discoverySource: string
  scenarios: string[]
  requiresAuth: boolean
  bizSuccess: BizSuccessRule | null
  perfBaseline: PerfBaseline | null
  customHeaders: Record<string, string>
  parameters: OpenApiParameter[]
  requestBody: Record<string, unknown> | null
  responses: Record<string, Record<string, unknown>>
}

const props = defineProps<{
  runId: string
}>()

const loading = ref(true)
const errorMessage = ref('')
const spec = ref<Record<string, unknown> | null>(null)
const searchQuery = ref('')
const methodFilter = ref('ALL')
const sourceFilter = ref('ALL')
const scenarioFilter = ref('ALL')
const viewMode = ref<'list' | 'scenario'>('list')
const expandedKeys = ref<Set<string>>(new Set())
const collapsedGroups = ref<Set<string>>(new Set())
const panelViewModes = ref<Record<string, 'example' | 'schema'>>({})
const copiedJsonKey = ref<string | null>(null)

const HTTP_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'] as const
const SOURCE_PRIORITY: Record<string, number> = {
  'har-live': 0,
  'js-static-inferred': 1,
  'js-static': 2,
}

async function loadSpec() {
  loading.value = true
  errorMessage.value = ''
  spec.value = null
  scenarioFilter.value = 'ALL'
  collapsedGroups.value = new Set()
  expandedKeys.value = new Set()
  try {
    const data = await getRunOpenApi(props.runId)
    spec.value = (data ?? null) as Record<string, unknown> | null
    const hasLive = endpoints.value.some((ep) => ep.discoverySource === 'har-live')
    sourceFilter.value = hasLive ? 'har-live' : 'ALL'
  } catch (err) {
    errorMessage.value =
      err instanceof Error ? err.message : t('openapi.emptyDesc')
  } finally {
    loading.value = false
  }
}

const specInfo = computed(() => {
  const info = (spec.value?.info ?? {}) as Record<string, unknown>
  const servers = (spec.value?.servers ?? []) as Array<Record<string, unknown>>
  const summary = (spec.value?.['x-reconqa-summary'] ?? {}) as Record<string, unknown>
  return {
    title: String(info.title ?? t('openapi.defaultTitle')),
    version: String(spec.value?.openapi ?? '3.0.3'),
    serverUrl: String(servers[0]?.url ?? ''),
    summary,
  }
})

const endpoints = computed<FlattenedEndpoint[]>(() => {
  const paths = (spec.value?.paths ?? {}) as Record<string, Record<string, Record<string, unknown>>>
  const rows: FlattenedEndpoint[] = []

  for (const [pathStr, pathItem] of Object.entries(paths)) {
    if (!pathItem || typeof pathItem !== 'object') continue
    for (const [methodKey, op] of Object.entries(pathItem)) {
      const upper = methodKey.toUpperCase()
      if (!['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'].includes(upper)) {
        continue
      }
      if (!op || typeof op !== 'object') continue

      const rawCustomHeaders = op['x-custom-headers']
      const normalizedCustomHeaders: Record<string, string> = Array.isArray(rawCustomHeaders)
        ? Object.fromEntries(rawCustomHeaders.map((h) => [String(h), '(present)']))
        : ((rawCustomHeaders as Record<string, string>) ?? {})

      rows.push({
        key: `${upper} ${pathStr}`,
        method: upper,
        path: pathStr,
        operationId: String(op.operationId ?? ''),
        summary: String(op.summary ?? `${upper} ${pathStr}`),
        tags: Array.isArray(op.tags) ? op.tags.map(String) : [],
        discoverySource: String(op['x-discovery-source'] ?? 'har-live'),
        scenarios: Array.isArray(op['x-scenarios']) ? op['x-scenarios'].map(String) : [],
        requiresAuth: Boolean(op['x-requires-auth']),
        bizSuccess: (op['x-biz-success'] as BizSuccessRule | null) ?? null,
        perfBaseline: (op['x-perf-baseline'] as PerfBaseline | null) ?? null,
        customHeaders: normalizedCustomHeaders,
        parameters: Array.isArray(op.parameters) ? (op.parameters as OpenApiParameter[]) : [],
        requestBody: (op.requestBody as Record<string, unknown> | null) ?? null,
        responses: (op.responses as Record<string, Record<string, unknown>>) ?? {},
      })
    }
  }

  return rows.sort((a, b) => {
    const pa = SOURCE_PRIORITY[a.discoverySource] ?? 3
    const pb = SOURCE_PRIORITY[b.discoverySource] ?? 3
    if (pa !== pb) return pa - pb
    return a.path.localeCompare(b.path) || a.method.localeCompare(b.method)
  })
})

const sourceCounts = computed(() => {
  const counts: Record<string, number> = {
    'har-live': 0,
    'js-static-inferred': 0,
    'js-static': 0,
  }
  for (const ep of endpoints.value) {
    counts[ep.discoverySource] = (counts[ep.discoverySource] ?? 0) + 1
  }
  return counts
})

const sourceScopedEndpoints = computed(() => {
  if (sourceFilter.value === 'ALL') return endpoints.value
  return endpoints.value.filter((ep) => ep.discoverySource === sourceFilter.value)
})

const scenarioPills = computed(() => {
  const allNames = new Set<string>()
  for (const ep of endpoints.value) {
    for (const sc of ep.scenarios) {
      if (sc) allNames.add(sc)
    }
  }
  if (allNames.size === 0) return []

  const counts = new Map<string, number>()
  for (const name of allNames) counts.set(name, 0)
  for (const ep of sourceScopedEndpoints.value) {
    for (const sc of ep.scenarios) {
      if (counts.has(sc)) {
        counts.set(sc, (counts.get(sc) ?? 0) + 1)
      }
    }
  }

  return Array.from(counts.entries())
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }))
    .map(([name, count]) => ({ name, count }))
})

const filteredEndpoints = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  return endpoints.value.filter((ep) => {
    if (methodFilter.value !== 'ALL' && ep.method !== methodFilter.value) {
      return false
    }
    if (sourceFilter.value !== 'ALL' && ep.discoverySource !== sourceFilter.value) {
      return false
    }
    if (scenarioFilter.value !== 'ALL' && !ep.scenarios.includes(scenarioFilter.value)) {
      return false
    }
    if (!q) return true
    return (
      ep.path.toLowerCase().includes(q) ||
      ep.summary.toLowerCase().includes(q) ||
      ep.operationId.toLowerCase().includes(q) ||
      ep.tags.some((tItem) => tItem.toLowerCase().includes(q)) ||
      ep.scenarios.some((s) => s.toLowerCase().includes(q))
    )
  })
})

interface EndpointDisplayGroup {
  id: string
  label: string
  showHeader: boolean
  endpoints: FlattenedEndpoint[]
  methodCounts: Array<{ method: string; count: number }>
}

function summarizeMethods(list: FlattenedEndpoint[]): Array<{ method: string; count: number }> {
  const order = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
  const map = new Map<string, number>()
  for (const ep of list) {
    map.set(ep.method, (map.get(ep.method) ?? 0) + 1)
  }
  return Array.from(map.entries())
    .sort(([a], [b]) => {
      const ia = order.indexOf(a)
      const ib = order.indexOf(b)
      if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
      return a.localeCompare(b)
    })
    .map(([method, count]) => ({ method, count }))
}

const displayGroups = computed<EndpointDisplayGroup[]>(() => {
  if (viewMode.value === 'list') {
    return [
      {
        id: '__list__',
        label: '',
        showHeader: false,
        endpoints: filteredEndpoints.value,
        methodCounts: [],
      },
    ]
  }

  const byScenario = new Map<string, FlattenedEndpoint[]>()
  const unassigned: FlattenedEndpoint[] = []

  for (const ep of filteredEndpoints.value) {
    const matchedScenarios =
      scenarioFilter.value !== 'ALL'
        ? ep.scenarios.filter((s) => s === scenarioFilter.value)
        : ep.scenarios
    if (matchedScenarios.length === 0) {
      unassigned.push(ep)
    } else {
      for (const sc of matchedScenarios) {
        const bucket = byScenario.get(sc) ?? []
        bucket.push(ep)
        byScenario.set(sc, bucket)
      }
    }
  }

  const groups: EndpointDisplayGroup[] = Array.from(byScenario.entries())
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }))
    .map(([name, list]) => ({
      id: `sc:${name}`,
      label: name,
      showHeader: true,
      endpoints: list,
      methodCounts: summarizeMethods(list),
    }))

  if (unassigned.length > 0) {
    groups.push({
      id: '__unassigned__',
      label: t('openapi.unassignedScenario'),
      showHeader: true,
      endpoints: unassigned,
      methodCounts: summarizeMethods(unassigned),
    })
  }

  return groups
})

function toggleGroupCollapse(groupId: string) {
  const next = new Set(collapsedGroups.value)
  if (next.has(groupId)) next.delete(groupId)
  else next.add(groupId)
  collapsedGroups.value = next
}

function toggleScenarioFilter(name: string) {
  scenarioFilter.value = scenarioFilter.value === name ? 'ALL' : name
}

function toggleExpand(key: string) {
  const next = new Set(expandedKeys.value)
  if (next.has(key)) {
    next.delete(key)
  } else {
    next.add(key)
  }
  expandedKeys.value = next
}

function methodBadgeClass(method: string): string {
  switch (method) {
    case 'GET':
      return 'bg-sky-50 text-sky-700 ring-sky-600/20'
    case 'POST':
      return 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
    case 'PUT':
      return 'bg-amber-50 text-amber-700 ring-amber-600/20'
    case 'PATCH':
      return 'bg-violet-50 text-violet-700 ring-violet-600/20'
    case 'DELETE':
      return 'bg-rose-50 text-rose-700 ring-rose-600/20'
    default:
      return 'bg-slate-100 text-slate-700 ring-slate-600/20'
  }
}

function extractJsonSchema(container: Record<string, unknown> | null | undefined): unknown {
  if (!container) return null
  const content = container.content as Record<string, Record<string, unknown>> | undefined
  if (!content) return null
  const jsonContent = content['application/json'] ?? Object.values(content)[0]
  return jsonContent?.schema ?? null
}

function extractJsonExample(container: Record<string, unknown> | null | undefined): unknown {
  if (!container) return null
  const content = container.content as Record<string, Record<string, unknown>> | undefined
  if (!content) return null
  const jsonContent = content['application/json'] ?? Object.values(content)[0]
  if (!jsonContent) return null
  if (jsonContent.example !== undefined && jsonContent.example !== null) {
    return jsonContent.example
  }
  const examples = jsonContent.examples as Record<string, unknown> | undefined
  if (examples && typeof examples === 'object') {
    const first = Object.values(examples)[0]
    if (first && typeof first === 'object' && 'value' in (first as Record<string, unknown>)) {
      return (first as Record<string, unknown>).value
    }
    if (first !== undefined && first !== null) {
      return first
    }
  }
  return null
}

function getPanelMode(
  epKey: string,
  section: 'req' | 'resp',
  hasExample: boolean,
): 'example' | 'schema' {
  const mapKey = `${epKey}::${section}`
  const explicit = panelViewModes.value[mapKey]
  if (explicit) return explicit
  return hasExample ? 'example' : 'schema'
}

function setPanelMode(epKey: string, section: 'req' | 'resp', mode: 'example' | 'schema') {
  panelViewModes.value = {
    ...panelViewModes.value,
    [`${epKey}::${section}`]: mode,
  }
}

async function copyJsonPayload(copyKey: string, payload: unknown) {
  if (payload === null || payload === undefined) return
  try {
    await navigator.clipboard.writeText(prettyJson(payload))
    copiedJsonKey.value = copyKey
    window.setTimeout(() => {
      if (copiedJsonKey.value === copyKey) {
        copiedJsonKey.value = null
      }
    }, 1800)
  } catch {
    copiedJsonKey.value = copyKey
  }
}

function getPrimaryResponse(responses: Record<string, Record<string, unknown>>): {
  status: string
  response: Record<string, unknown> | null
} {
  if (responses['200']) return { status: '200', response: responses['200'] }
  const first2xx = Object.keys(responses).find((k) => k.startsWith('2'))
  if (first2xx) return { status: first2xx, response: responses[first2xx] }
  const firstKey = Object.keys(responses)[0]
  return { status: firstKey ?? '200', response: firstKey ? responses[firstKey] : null }
}

function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function formatBytes(bytes?: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  return `${(bytes / 1024).toFixed(1)} KB`
}

watch(() => props.runId, loadSpec)
onMounted(loadSpec)
</script>

<template>
  <div class="space-y-4">
    <!-- Loading -->
    <div
      v-if="loading"
      class="flex items-center justify-center rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500"
    >
      {{ t('openapi.loading') }}
    </div>

    <!-- Empty / Error -->
    <EmptyState
      v-else-if="!spec || endpoints.length === 0"
      :title="t('openapi.emptyTitle')"
      :description="errorMessage || t('openapi.emptyDesc')"
    />

    <!-- Spec Explorer -->
    <template v-else>
      <!-- Top Summary & Downloads -->
      <div class="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
        <div class="space-y-1">
          <div class="flex flex-wrap items-center gap-2.5">
            <FileCode2 class="h-5 w-5 text-indigo-600" />
            <h3 class="text-base font-semibold text-slate-900">{{ specInfo.title }}</h3>
            <span class="rounded-md bg-slate-100 px-2 py-0.5 font-mono text-xs font-medium text-slate-700">
              OpenAPI {{ specInfo.version }}
            </span>
            <span
              v-if="specInfo.serverUrl"
              class="rounded-md bg-indigo-50 px-2 py-0.5 font-mono text-xs text-indigo-700"
            >
              {{ specInfo.serverUrl }}
            </span>
          </div>
          <div class="flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span>{{ t('openapi.totalOps') }} <strong class="text-slate-800">{{ endpoints.length }}</strong></span>
            <span>·</span>
            <span>har-live: <strong class="text-emerald-700">{{ sourceCounts['har-live'] }}</strong></span>
            <span>·</span>
            <span>js-static-inferred: <strong class="text-amber-700">{{ sourceCounts['js-static-inferred'] }}</strong></span>
            <span>·</span>
            <span>js-static: <strong class="text-slate-700">{{ sourceCounts['js-static'] }}</strong></span>
          </div>
        </div>

        <div class="flex items-center gap-2">
          <a
            :href="artifactDownloadUrl(runId, 'openapi_yaml')"
            download
            class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
          >
            <Download class="h-3.5 w-3.5 text-indigo-600" />
            {{ t('openapi.downloadYaml') }}
          </a>
          <a
            :href="artifactDownloadUrl(runId, 'openapi_json')"
            download
            class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-50"
          >
            <Download class="h-3.5 w-3.5 text-slate-600" />
            {{ t('openapi.downloadJson') }}
          </a>
        </div>
      </div>

      <!-- Filter Bar -->
      <div class="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
        <div class="relative min-w-[240px] flex-1">
          <Search class="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            v-model="searchQuery"
            type="text"
            :placeholder="t('openapi.searchPlaceholder')"
            class="w-full rounded-lg border border-slate-300 py-1.5 pr-3 pl-9 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
          />
        </div>

        <!-- Method filter -->
        <div class="flex items-center gap-1 rounded-lg bg-slate-100 p-1">
          <button
            type="button"
            class="rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="methodFilter === 'ALL' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600 hover:text-slate-900'"
            @click="methodFilter = 'ALL'"
          >
            {{ t('openapi.allMethods') }}
          </button>
          <button
            v-for="m in HTTP_METHODS"
            :key="m"
            type="button"
            class="rounded-md px-2 py-1 font-mono text-xs font-medium transition"
            :class="methodFilter === m ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600 hover:text-slate-900'"
            @click="methodFilter = m"
          >
            {{ m }}
          </button>
        </div>

        <!-- Discovery source filter (har-live prioritized first) -->
        <div class="flex flex-wrap items-center gap-1 rounded-lg bg-slate-100 p-1">
          <button
            type="button"
            data-testid="openapi-source-har-live"
            class="rounded-md px-2.5 py-1 text-xs font-semibold transition"
            :class="
              sourceFilter === 'har-live'
                ? 'bg-emerald-600 text-white shadow-sm'
                : 'text-emerald-700 hover:bg-emerald-50'
            "
            @click="sourceFilter = 'har-live'"
          >
            {{ t('openapi.source.harLive') }} ({{ sourceCounts['har-live'] }})
          </button>
          <button
            type="button"
            data-testid="openapi-source-js-static-inferred"
            class="rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="
              sourceFilter === 'js-static-inferred'
                ? 'bg-white text-amber-800 shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            "
            @click="sourceFilter = 'js-static-inferred'"
          >
            {{ t('openapi.source.jsInferred') }} ({{ sourceCounts['js-static-inferred'] }})
          </button>
          <button
            type="button"
            data-testid="openapi-source-js-static"
            class="rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="
              sourceFilter === 'js-static'
                ? 'bg-white text-slate-900 shadow-sm'
                : 'text-slate-500 hover:text-slate-800'
            "
            @click="sourceFilter = 'js-static'"
          >
            {{ t('openapi.source.jsStatic') }} ({{ sourceCounts['js-static'] }})
          </button>
          <button
            type="button"
            data-testid="openapi-source-ALL"
            class="rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="
              sourceFilter === 'ALL'
                ? 'bg-white text-slate-900 shadow-sm'
                : 'text-slate-500 hover:text-slate-900'
            "
            @click="sourceFilter = 'ALL'"
          >
            {{ t('openapi.allSources') }} ({{ endpoints.length }})
          </button>
        </div>

        <!-- View Mode Toggle: List | By Scenario -->
        <div
          v-if="scenarioPills.length > 0"
          class="ml-auto inline-flex items-center gap-1 rounded-lg bg-slate-100 p-1"
        >
          <button
            type="button"
            data-testid="openapi-view-mode-list"
            class="inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="
              viewMode === 'list'
                ? 'bg-white text-indigo-700 shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            "
            @click="viewMode = 'list'"
          >
            <List class="h-3.5 w-3.5" />
            <span>{{ t('openapi.viewModeList') }}</span>
          </button>
          <button
            type="button"
            data-testid="openapi-view-mode-scenario"
            class="inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="
              viewMode === 'scenario'
                ? 'bg-white text-indigo-700 shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            "
            @click="viewMode = 'scenario'"
          >
            <Layers class="h-3.5 w-3.5" />
            <span>{{ t('openapi.viewModeScenario') }}</span>
          </button>
        </div>
      </div>

      <!-- Dedicated Business Scenarios Filter Bar -->
      <div
        v-if="scenarioPills.length > 0"
        data-testid="scenario-filter-bar"
        class="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm"
      >
        <div class="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-600">
          <Layers class="h-3.5 w-3.5 text-indigo-600" />
          <span>{{ t('openapi.scenariosFilterLabel') }}:</span>
        </div>

        <button
          type="button"
          data-testid="scenario-pill-all"
          class="rounded-full px-2.5 py-1 text-xs font-medium transition"
          :class="
            scenarioFilter === 'ALL'
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'bg-slate-100 text-slate-600 hover:bg-slate-200 hover:text-slate-900'
          "
          @click="scenarioFilter = 'ALL'"
        >
          {{ t('openapi.allScenarios') }} ({{ sourceScopedEndpoints.length }})
        </button>

        <button
          v-for="sc in scenarioPills"
          :key="sc.name"
          type="button"
          :data-testid="`scenario-pill-${sc.name}`"
          class="rounded-full px-2.5 py-1 font-mono text-xs font-medium transition"
          :class="
            scenarioFilter === sc.name
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'bg-indigo-50/70 text-indigo-700 ring-1 ring-inset ring-indigo-200/80 hover:bg-indigo-100'
          "
          @click="toggleScenarioFilter(sc.name)"
        >
          {{ sc.name }} ({{ sc.count }})
        </button>
      </div>

      <!-- Empty state when grouped view has no matches -->
      <div
        v-if="displayGroups.length === 0"
        class="rounded-xl border border-slate-200 bg-white px-6 py-10 text-center text-sm text-slate-500 shadow-sm"
      >
        {{ t('openapi.noMatches') }}
      </div>

      <!-- Interactive Table(s) — Single Card in List Mode, Collapsible Scenario Cards in Grouped Mode -->
      <div v-else class="space-y-4">
        <div
          v-for="group in displayGroups"
          :key="group.id"
          :data-testid="group.showHeader ? 'scenario-group-card' : 'openapi-list-card'"
          class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm"
        >
          <!-- Scenario Group Header -->
          <button
            v-if="group.showHeader"
            type="button"
            class="flex w-full flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50/80 px-4 py-3 text-left transition hover:bg-slate-100/70"
            @click="toggleGroupCollapse(group.id)"
          >
            <div class="flex min-w-0 items-center gap-2">
              <ChevronRight
                v-if="collapsedGroups.has(group.id)"
                class="h-4 w-4 shrink-0 text-slate-400"
              />
              <ChevronDown v-else class="h-4 w-4 shrink-0 text-indigo-600" />
              <Layers class="h-4 w-4 shrink-0 text-indigo-600" />
              <span class="truncate font-mono text-sm font-semibold text-slate-900">
                {{ group.label }}
              </span>
              <span class="rounded-full bg-indigo-50 px-2 py-0.5 text-[11px] font-semibold text-indigo-700">
                {{ t('openapi.scenarioGroupCount', { count: group.endpoints.length }) }}
              </span>
            </div>

            <div class="flex flex-wrap items-center gap-1.5">
              <span
                v-for="mc in group.methodCounts"
                :key="mc.method"
                class="inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ring-1 ring-inset"
                :class="methodBadgeClass(mc.method)"
              >
                {{ mc.method }} {{ mc.count }}
              </span>
            </div>
          </button>

          <div v-if="!group.showHeader || !collapsedGroups.has(group.id)" class="overflow-x-auto">
            <table class="min-w-full divide-y divide-slate-200 text-left text-sm">
              <thead class="bg-slate-50 text-xs font-semibold tracking-wider text-slate-500 uppercase">
                <tr>
                  <th class="w-9 px-3 py-3" />
                  <th class="w-24 px-3 py-3">{{ t('openapi.col.method') }}</th>
                  <th class="px-3 py-3">{{ t('openapi.col.path') }}</th>
                  <th class="px-3 py-3">{{ t('openapi.col.source') }}</th>
                  <th class="px-3 py-3">{{ t('openapi.col.scenarios') }}</th>
                  <th class="px-3 py-3 text-right">{{ t('openapi.col.perf') }}</th>
                  <th class="w-16 px-3 py-3 text-center">{{ t('openapi.col.auth') }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100">
                <template v-for="ep in group.endpoints" :key="`${group.id}::${ep.key}`">
                  <tr
                    class="cursor-pointer transition hover:bg-slate-50/90"
                    :class="expandedKeys.has(`${group.id}::${ep.key}`) ? 'bg-indigo-50/40' : ''"
                    @click="toggleExpand(`${group.id}::${ep.key}`)"
                  >
                    <td class="px-3 py-3 text-slate-400">
                      <ChevronDown
                        v-if="expandedKeys.has(`${group.id}::${ep.key}`)"
                        class="h-4 w-4 text-indigo-600"
                      />
                      <ChevronRight v-else class="h-4 w-4" />
                    </td>
                    <td class="px-3 py-3">
                      <span
                        class="inline-flex rounded-md px-2 py-0.5 font-mono text-xs font-semibold ring-1 ring-inset"
                        :class="methodBadgeClass(ep.method)"
                      >
                        {{ ep.method }}
                      </span>
                    </td>
                    <td class="px-3 py-3">
                      <div class="font-mono text-xs font-medium text-slate-900">{{ ep.path }}</div>
                      <div v-if="ep.operationId" class="mt-0.5 text-[11px] text-slate-400">
                        {{ ep.operationId }}
                      </div>
                    </td>
                    <td class="px-3 py-3">
                      <StatusBadge :status="ep.discoverySource" size="sm" />
                    </td>
                    <td class="px-3 py-3">
                      <div class="flex flex-wrap gap-1">
                        <span
                          v-for="sc in ep.scenarios"
                          :key="sc"
                          class="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-600"
                        >
                          {{ sc }}
                        </span>
                        <span v-if="ep.scenarios.length === 0" class="text-xs text-slate-400">—</span>
                      </div>
                    </td>
                    <td class="px-3 py-3 text-right font-mono text-xs text-slate-600">
                      <template v-if="ep.perfBaseline && (ep.perfBaseline.count ?? 0) > 0">
                        <span>p50 {{ ep.perfBaseline.p50_ms }}ms</span>
                        <span class="mx-1 text-slate-300">/</span>
                        <span class="text-slate-900">p95 {{ ep.perfBaseline.p95_ms }}ms</span>
                      </template>
                      <span v-else class="text-slate-400">—</span>
                    </td>
                    <td class="px-3 py-3 text-center">
                      <Lock
                        v-if="ep.requiresAuth"
                        class="mx-auto h-3.5 w-3.5 text-amber-600"
                        :title="t('openapi.requiresAuthTitle')"
                      />
                      <span v-else class="text-xs text-slate-300">—</span>
                    </td>
                  </tr>

                  <!-- Expanded Operation Drawer -->
                  <tr v-if="expandedKeys.has(`${group.id}::${ep.key}`)" class="bg-slate-50/70">
                  <td colspan="7" class="px-6 py-4">
                    <div class="space-y-4">
                      <!-- Metadata strip -->
                      <div class="flex flex-wrap items-center gap-3 text-xs">
                        <div
                          v-if="ep.bizSuccess"
                          class="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-emerald-800"
                        >
                          <span class="font-semibold">x-biz-success:</span>
                          <code class="font-mono">{{ ep.bizSuccess.field }} == {{ JSON.stringify(ep.bizSuccess.value) }}</code>
                        </div>

                        <div
                          v-if="ep.perfBaseline"
                          class="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-slate-700"
                        >
                          <span class="font-semibold text-slate-500">x-perf-baseline:</span>
                          <span class="font-mono">n={{ ep.perfBaseline.count ?? 0 }}</span>
                          <span class="font-mono">p50={{ ep.perfBaseline.p50_ms ?? 0 }}ms</span>
                          <span class="font-mono">p95={{ ep.perfBaseline.p95_ms ?? 0 }}ms</span>
                          <span class="font-mono">avg={{ formatBytes(ep.perfBaseline.avg_bytes) }}</span>
                        </div>

                        <div v-if="ep.scenarios.length > 0" class="inline-flex items-center gap-1.5">
                          <span class="font-semibold text-slate-500">{{ t('openapi.scenariosLabel') }}</span>
                          <span
                            v-for="sc in ep.scenarios"
                            :key="sc"
                            class="rounded-md border border-indigo-200 bg-indigo-50 px-2 py-0.5 font-medium text-indigo-700"
                          >
                            {{ sc }}
                          </span>
                        </div>
                      </div>

                      <!-- Parameters & Custom Headers -->
                      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
                        <div class="rounded-lg border border-slate-200 bg-white p-3.5">
                          <h4 class="mb-2 text-xs font-semibold tracking-wide text-slate-600 uppercase">
                            {{ t('openapi.paramsTitle', { count: ep.parameters.length }) }}
                          </h4>
                          <div v-if="ep.parameters.length === 0" class="text-xs text-slate-400">
                            {{ t('openapi.noParams') }}
                          </div>
                          <table v-else class="w-full text-left text-xs">
                            <thead class="border-b border-slate-100 text-slate-400">
                              <tr>
                                <th class="pb-1.5 font-medium">{{ t('openapi.paramName') }}</th>
                                <th class="pb-1.5 font-medium">{{ t('openapi.paramIn') }}</th>
                                <th class="pb-1.5 font-medium">{{ t('openapi.paramType') }}</th>
                                <th class="pb-1.5 font-medium">{{ t('openapi.paramExample') }}</th>
                              </tr>
                            </thead>
                            <tbody class="divide-y divide-slate-100">
                              <tr v-for="p in ep.parameters" :key="`${p.in}-${p.name}`">
                                <td class="py-1.5 font-mono font-medium text-slate-800">
                                  {{ p.name }}<span v-if="p.required" class="text-rose-500">*</span>
                                </td>
                                <td class="py-1.5 text-slate-500">{{ p.in }}</td>
                                <td class="py-1.5 font-mono text-slate-600">{{ p.schema?.type ?? 'string' }}</td>
                                <td class="py-1.5 font-mono text-slate-500">{{ p.example ?? '—' }}</td>
                              </tr>
                            </tbody>
                          </table>
                        </div>

                        <div class="rounded-lg border border-slate-200 bg-white p-3.5">
                          <h4 class="mb-2 text-xs font-semibold tracking-wide text-slate-600 uppercase">
                            {{ t('openapi.customHeadersTitle', { count: Object.keys(ep.customHeaders).length }) }}
                          </h4>
                          <div
                            v-if="Object.keys(ep.customHeaders).length === 0"
                            class="text-xs text-slate-400"
                          >
                            {{ t('openapi.noCustomHeaders') }}
                          </div>
                          <dl v-else class="space-y-1 text-xs">
                            <div
                              v-for="(val, headerName) in ep.customHeaders"
                              :key="headerName"
                              class="flex items-start justify-between gap-2 rounded bg-slate-50 px-2 py-1"
                            >
                              <dt class="font-mono font-medium text-slate-700">{{ headerName }}</dt>
                              <dd class="truncate font-mono text-slate-500">{{ val }}</dd>
                            </div>
                          </dl>
                        </div>
                      </div>

                      <!-- Request Body & 200 Response (Example | Schema toggle + Copy JSON) -->
                      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
                        <!-- Request Body Card -->
                        <div class="rounded-lg border border-slate-200 bg-white p-3.5">
                          <div class="mb-2.5 flex flex-wrap items-center justify-between gap-2">
                            <h4 class="text-xs font-semibold tracking-wide text-slate-600 uppercase">
                              {{ t('openapi.reqBodyTitle') }}
                            </h4>
                            <div
                              v-if="extractJsonExample(ep.requestBody) !== null || extractJsonSchema(ep.requestBody)"
                              class="flex items-center gap-1.5"
                            >
                              <div class="inline-flex rounded-md bg-slate-100 p-0.5 text-[11px]">
                                <button
                                  type="button"
                                  data-testid="openapi-req-example-tab"
                                  class="rounded px-2 py-0.5 font-medium transition"
                                  :class="
                                    getPanelMode(ep.key, 'req', extractJsonExample(ep.requestBody) !== null) === 'example'
                                      ? 'bg-white text-indigo-700 shadow-sm'
                                      : 'text-slate-500 hover:text-slate-800'
                                  "
                                  @click="setPanelMode(ep.key, 'req', 'example')"
                                >
                                  {{ t('openapi.viewExample') }}
                                </button>
                                <button
                                  type="button"
                                  data-testid="openapi-req-schema-tab"
                                  class="rounded px-2 py-0.5 font-medium transition"
                                  :class="
                                    getPanelMode(ep.key, 'req', extractJsonExample(ep.requestBody) !== null) === 'schema'
                                      ? 'bg-white text-indigo-700 shadow-sm'
                                      : 'text-slate-500 hover:text-slate-800'
                                  "
                                  @click="setPanelMode(ep.key, 'req', 'schema')"
                                >
                                  {{ t('openapi.viewSchema') }}
                                </button>
                              </div>
                              <button
                                v-if="
                                  (getPanelMode(ep.key, 'req', extractJsonExample(ep.requestBody) !== null) === 'example'
                                    ? extractJsonExample(ep.requestBody)
                                    : extractJsonSchema(ep.requestBody)) !== null
                                "
                                type="button"
                                data-testid="openapi-req-copy-btn"
                                class="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-600 transition hover:bg-slate-50 hover:text-slate-900"
                                @click="
                                  copyJsonPayload(
                                    `${ep.key}::req`,
                                    getPanelMode(ep.key, 'req', extractJsonExample(ep.requestBody) !== null) === 'example'
                                      ? extractJsonExample(ep.requestBody)
                                      : extractJsonSchema(ep.requestBody),
                                  )
                                "
                              >
                                <Check v-if="copiedJsonKey === `${ep.key}::req`" class="h-3 w-3 text-emerald-600" />
                                <Copy v-else class="h-3 w-3" />
                                <span>
                                  {{
                                    copiedJsonKey === `${ep.key}::req`
                                      ? t('openapi.copiedJson')
                                      : t('openapi.copyJson')
                                  }}
                                </span>
                              </button>
                            </div>
                          </div>

                          <div
                            v-if="extractJsonExample(ep.requestBody) === null && !extractJsonSchema(ep.requestBody)"
                            class="text-xs text-slate-400"
                          >
                            {{ t('openapi.noReqBody') }}
                          </div>
                          <template
                            v-else-if="getPanelMode(ep.key, 'req', extractJsonExample(ep.requestBody) !== null) === 'example'"
                          >
                            <div
                              v-if="extractJsonExample(ep.requestBody) === null"
                              class="text-xs text-slate-400"
                            >
                              {{ t('openapi.noReqExample') }}
                            </div>
                            <pre
                              v-else
                              data-testid="openapi-req-example-json"
                              class="max-h-64 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-sky-200"
                            >{{ prettyJson(extractJsonExample(ep.requestBody)) }}</pre>
                          </template>
                          <template v-else>
                            <div
                              v-if="!extractJsonSchema(ep.requestBody)"
                              class="text-xs text-slate-400"
                            >
                              {{ t('openapi.noReqBody') }}
                            </div>
                            <pre
                              v-else
                              class="max-h-64 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-slate-100"
                            >{{ prettyJson(extractJsonSchema(ep.requestBody)) }}</pre>
                          </template>
                        </div>

                        <!-- Response Card -->
                        <div class="rounded-lg border border-slate-200 bg-white p-3.5">
                          <div class="mb-2.5 flex flex-wrap items-center justify-between gap-2">
                            <h4 class="text-xs font-semibold tracking-wide text-slate-600 uppercase">
                              {{ t('openapi.respSchemaTitle', { status: getPrimaryResponse(ep.responses).status }) }}
                            </h4>
                            <div
                              v-if="
                                extractJsonExample(getPrimaryResponse(ep.responses).response) !== null ||
                                extractJsonSchema(getPrimaryResponse(ep.responses).response)
                              "
                              class="flex items-center gap-1.5"
                            >
                              <div class="inline-flex rounded-md bg-slate-100 p-0.5 text-[11px]">
                                <button
                                  type="button"
                                  data-testid="openapi-resp-example-tab"
                                  class="rounded px-2 py-0.5 font-medium transition"
                                  :class="
                                    getPanelMode(
                                      ep.key,
                                      'resp',
                                      extractJsonExample(getPrimaryResponse(ep.responses).response) !== null,
                                    ) === 'example'
                                      ? 'bg-white text-emerald-700 shadow-sm'
                                      : 'text-slate-500 hover:text-slate-800'
                                  "
                                  @click="setPanelMode(ep.key, 'resp', 'example')"
                                >
                                  {{ t('openapi.viewExample') }}
                                </button>
                                <button
                                  type="button"
                                  data-testid="openapi-resp-schema-tab"
                                  class="rounded px-2 py-0.5 font-medium transition"
                                  :class="
                                    getPanelMode(
                                      ep.key,
                                      'resp',
                                      extractJsonExample(getPrimaryResponse(ep.responses).response) !== null,
                                    ) === 'schema'
                                      ? 'bg-white text-indigo-700 shadow-sm'
                                      : 'text-slate-500 hover:text-slate-800'
                                  "
                                  @click="setPanelMode(ep.key, 'resp', 'schema')"
                                >
                                  {{ t('openapi.viewSchema') }}
                                </button>
                              </div>
                              <button
                                v-if="
                                  (getPanelMode(
                                    ep.key,
                                    'resp',
                                    extractJsonExample(getPrimaryResponse(ep.responses).response) !== null,
                                  ) === 'example'
                                    ? extractJsonExample(getPrimaryResponse(ep.responses).response)
                                    : extractJsonSchema(getPrimaryResponse(ep.responses).response)) !== null
                                "
                                type="button"
                                data-testid="openapi-resp-copy-btn"
                                class="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-600 transition hover:bg-slate-50 hover:text-slate-900"
                                @click="
                                  copyJsonPayload(
                                    `${ep.key}::resp`,
                                    getPanelMode(
                                      ep.key,
                                      'resp',
                                      extractJsonExample(getPrimaryResponse(ep.responses).response) !== null,
                                    ) === 'example'
                                      ? extractJsonExample(getPrimaryResponse(ep.responses).response)
                                      : extractJsonSchema(getPrimaryResponse(ep.responses).response),
                                  )
                                "
                              >
                                <Check v-if="copiedJsonKey === `${ep.key}::resp`" class="h-3 w-3 text-emerald-600" />
                                <Copy v-else class="h-3 w-3" />
                                <span>
                                  {{
                                    copiedJsonKey === `${ep.key}::resp`
                                      ? t('openapi.copiedJson')
                                      : t('openapi.copyJson')
                                  }}
                                </span>
                              </button>
                            </div>
                          </div>

                          <div
                            v-if="
                              extractJsonExample(getPrimaryResponse(ep.responses).response) === null &&
                              !extractJsonSchema(getPrimaryResponse(ep.responses).response)
                            "
                            class="text-xs text-slate-400"
                          >
                            {{ t('openapi.noRespSchema') }}
                          </div>
                          <template
                            v-else-if="
                              getPanelMode(
                                ep.key,
                                'resp',
                                extractJsonExample(getPrimaryResponse(ep.responses).response) !== null,
                              ) === 'example'
                            "
                          >
                            <div
                              v-if="extractJsonExample(getPrimaryResponse(ep.responses).response) === null"
                              class="text-xs text-slate-400"
                            >
                              {{ t('openapi.noRespExample') }}
                            </div>
                            <pre
                              v-else
                              data-testid="openapi-resp-example-json"
                              class="max-h-64 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-emerald-200"
                            >{{ prettyJson(extractJsonExample(getPrimaryResponse(ep.responses).response)) }}</pre>
                          </template>
                          <template v-else>
                            <div
                              v-if="!extractJsonSchema(getPrimaryResponse(ep.responses).response)"
                              class="text-xs text-slate-400"
                            >
                              {{ t('openapi.noRespSchema') }}
                            </div>
                            <pre
                              v-else
                              class="max-h-64 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-slate-100"
                            >{{ prettyJson(extractJsonSchema(getPrimaryResponse(ep.responses).response)) }}</pre>
                          </template>
                        </div>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>

                <tr v-if="group.endpoints.length === 0">
                  <td colspan="7" class="px-6 py-10 text-center text-sm text-slate-500">
                    {{ t('openapi.noMatches') }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
