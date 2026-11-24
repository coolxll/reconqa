<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowRight,
  Check,
  Code2,
  Compass,
  Copy,
  FileUp,
  FolderPlus,
  Globe,
  History,
  Info,
  Play,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Sparkles,
  Terminal,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-vue-next'
import type { Project, Run, Target } from '@/api/types'
import {
  createProject,
  createTarget,
  listProjects,
  listTargets,
} from '@/api/projects'
import {
  createRun,
  deleteRun,
  getRunTitle,
  importRunText,
  listProjectRuns,
  reuseRunHars,
  shortRunId,
  startRun,
  uploadRunHars,
} from '@/api/runs'
import StatusBadge from '@/components/layout/StatusBadge.vue'
import EmptyState from '@/components/layout/EmptyState.vue'
import { useWorkspace } from '@/stores/workspace'
import '@/i18n/locales/runs'
import { useI18n } from '@/i18n'

const { locale, t } = useI18n()
const {
  activeProjectId,
  ensureLoaded: ensureWorkspaceLoaded,
  upsertProject,
  setActiveProject,
} = useWorkspace()

interface RunWithProject extends Run {
  project_name: string
}

interface DetectedOrigin {
  host: string
  baseUrl: string
}

const MITM_HINT_CMD = 'mitmdump --set hardump=./manual_smoke.har -p 8080'

const SAMPLE_CURL_SNIPPET = `curl 'https://api.example.com/api/v1/products?category=electronics&page=1' \\
  -H 'Accept: application/json' \\
  -H 'Authorization: Bearer example-account-a-token' \\
  -H 'X-Client-Channel: web'

curl 'https://api.example.com/api/v1/cart/items' \\
  -X 'POST' \\
  -H 'Accept: application/json' \\
  -H 'Content-Type: application/json' \\
  -H 'Authorization: Bearer example-account-a-token' \\
  --data-raw '{"skuId":"SKU-1001","quantity":2}'`

const STATUS_FILTER_KEYS = ['all', 'running', 'queued', 'completed', 'failed', 'pending'] as const

const route = useRoute()
const router = useRouter()

const routeProjectId = computed(() =>
  typeof route.params.projectId === 'string' ? route.params.projectId : '',
)
const scopedProjectId = computed(() => routeProjectId.value || '')

const loading = ref(true)
const errorMessage = ref('')
const projects = ref<Project[]>([])
const runs = ref<RunWithProject[]>([])
const selectedProjectFilter = ref<string>('all')
const statusFilter = ref<string>('all')
const runSearch = ref('')
const confirmDeleteRunId = ref<string | null>(null)
const deletingRunId = ref<string | null>(null)

// Minimal 2-Step New Analysis Drawer state (Core Ingest/Merge only; capabilities run on demand in RunDetail)
const drawerOpen = ref(false)
const harInputMode = ref<'upload' | 'paste' | 'reuse'>('upload')
const uploadFiles = ref<File[]>([])
const uploadScenario = ref('')
const harDetectedOrigin = ref<DetectedOrigin | null>(null)
const pasteContent = ref('')
const pasteScenario = ref('01_devtools')
const pasteFormat = ref<'auto' | 'curl' | 'fetch' | 'raw_http' | 'har_json'>('auto')
const reuseSourceRunId = ref('')
const reuseScenariosText = ref('')
const isDragging = ref(false)
const copiedMitm = ref(false)

// Step 2: Flexible 3-mode Project Assignment
const projectMode = ref<'auto' | 'new' | 'existing'>('auto')
const inlineNewProjectName = ref('')
const wizardProjectId = ref('')
const wizardTargets = ref<Target[]>([])
const wizardProjectRuns = ref<Run[]>([])
const loadingProjectContext = ref(false)
const wizardTargetId = ref('')
const customRunName = ref('')

const submitting = ref(false)
const submitStepLabel = ref('')
const wizardError = ref('')

const projectMap = computed(() => {
  const map = new Map<string, Project>()
  for (const p of projects.value) {
    map.set(p.id, p)
  }
  return map
})

function parseOriginFromText(raw: string): DetectedOrigin | null {
  if (!raw.trim()) return null
  const urlMatch = raw.match(/(https?):\/\/([^/:\s'"`\\)]+)(?::(\d+))?/i)
  if (urlMatch) {
    const scheme = urlMatch[1].toLowerCase()
    const host = urlMatch[2].toLowerCase()
    const port = urlMatch[3] ? `:${urlMatch[3]}` : ''
    return { host, baseUrl: `${scheme}://${host}${port}` }
  }
  const hostHeaderMatch = raw.match(/^\s*Host:\s*([^\s/:]+)(?::(\d+))?/im)
  if (hostHeaderMatch) {
    const host = hostHeaderMatch[1].toLowerCase()
    const port = hostHeaderMatch[2] ? `:${hostHeaderMatch[2]}` : ''
    return { host, baseUrl: `https://${host}${port}` }
  }
  return null
}

function toSlug(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

function findMatchingProject(candidateName: string): Project | null {
  const norm = candidateName.trim().toLowerCase()
  const normSlug = toSlug(candidateName)
  if (!norm) return null
  for (const p of projects.value) {
    if (p.name.trim().toLowerCase() === norm) return p
    if (normSlug && p.slug.toLowerCase() === normSlug) return p
  }
  return null
}

const detectedOrigin = computed<DetectedOrigin | null>(() => {
  if (harInputMode.value === 'paste') {
    return parseOriginFromText(pasteContent.value)
  }
  if (harInputMode.value === 'upload') {
    return harDetectedOrigin.value
  }
  return null
})

const autoResolvedProjectName = computed(() => {
  if (detectedOrigin.value?.host) return detectedOrigin.value.host
  return t('runs.wizard.project.quickFallback')
})

const autoMatchedProject = computed<Project | null>(() =>
  findMatchingProject(autoResolvedProjectName.value),
)

const filteredRuns = computed(() => {
  const q = runSearch.value.trim().toLowerCase()
  return runs.value.filter((r) => {
    if (selectedProjectFilter.value !== 'all' && r.project_id !== selectedProjectFilter.value) {
      return false
    }
    if (statusFilter.value !== 'all' && r.status !== statusFilter.value) {
      return false
    }
    if (q) {
      const title = getRunTitle(r, r.project_name).toLowerCase()
      const projName = (r.project_name || projectMap.value.get(r.project_id)?.name || '').toLowerCase()
      const targetUrl = String(r.config_snapshot?.target_url ?? '').toLowerCase()
      const idLower = r.id.toLowerCase()
      if (
        !title.includes(q) &&
        !projName.includes(q) &&
        !targetUrl.includes(q) &&
        !idLower.includes(q)
      ) {
        return false
      }
    }
    return true
  })
})

async function handleDeleteRun(runId: string) {
  if (deletingRunId.value) return
  deletingRunId.value = runId
  errorMessage.value = ''
  try {
    await deleteRun(runId)
    runs.value = runs.value.filter((r) => r.id !== runId)
    confirmDeleteRunId.value = null
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : t('runs.table.deleteError')
  } finally {
    deletingRunId.value = null
  }
}

async function loadProjectsAndRuns() {
  loading.value = true
  errorMessage.value = ''
  try {
    const projList = await listProjects()
    projects.value = projList

    const queryProj =
      routeProjectId.value ||
      (typeof route.query.projectId === 'string'
        ? route.query.projectId
        : typeof route.query.project === 'string'
          ? route.query.project
          : '')
    if (queryProj && projList.some((p) => p.id === queryProj)) {
      selectedProjectFilter.value = queryProj
    } else if (!routeProjectId.value) {
      selectedProjectFilter.value = 'all'
    }

    const targetProjects = routeProjectId.value
      ? projList.filter((p) => p.id === routeProjectId.value)
      : projList

    const runLists = await Promise.all(
      targetProjects.map(async (proj) => {
        try {
          const list = await listProjectRuns(proj.id)
          return list.map((r) => ({ ...r, project_name: proj.name }))
        } catch {
          return [] as RunWithProject[]
        }
      }),
    )

    runs.value = runLists.flat().sort((a, b) => {
      const ta = a.started_at ? new Date(a.started_at).getTime() : 0
      const tb = b.started_at ? new Date(b.started_at).getTime() : 0
      return tb - ta
    })
  } catch (err) {
    errorMessage.value =
      err instanceof Error ? err.message : t('runs.loadError')
  } finally {
    loading.value = false
  }
}

async function loadWizardProjectContext(projectId: string) {
  if (!projectId) {
    wizardTargets.value = []
    wizardProjectRuns.value = []
    return
  }
  loadingProjectContext.value = true
  try {
    const [targets, projRuns] = await Promise.all([
      listTargets(projectId),
      listProjectRuns(projectId),
    ])
    wizardTargets.value = targets
    wizardProjectRuns.value = projRuns
    wizardTargetId.value = targets[0]?.id ?? ''
    reuseSourceRunId.value = projRuns[0]?.id ?? ''
  } catch {
    wizardTargets.value = []
    wizardProjectRuns.value = []
  } finally {
    loadingProjectContext.value = false
  }
}

watch(wizardProjectId, (nextId) => {
  loadWizardProjectContext(nextId)
})

watch(harInputMode, (mode) => {
  if (mode === 'reuse' && projects.value.length > 0) {
    projectMode.value = 'existing'
    if (!wizardProjectId.value) {
      wizardProjectId.value = projects.value[0].id
    } else {
      loadWizardProjectContext(wizardProjectId.value)
    }
  }
})

watch(
  () => route.query.new,
  (val) => {
    if (val === '1' && !drawerOpen.value) {
      openWizard()
    }
  },
)

function openWizard() {
  wizardError.value = ''
  submitStepLabel.value = ''
  inlineNewProjectName.value = ''
  customRunName.value = ''
  const preferredProj =
    routeProjectId.value ||
    (selectedProjectFilter.value !== 'all' ? selectedProjectFilter.value : '') ||
    activeProjectId.value
  if (preferredProj && projects.value.some((p) => p.id === preferredProj)) {
    projectMode.value = 'existing'
    wizardProjectId.value = preferredProj
  } else {
    projectMode.value = 'auto'
    if (!wizardProjectId.value && projects.value.length > 0) {
      wizardProjectId.value = projects.value[0].id
    }
  }
  if (wizardProjectId.value) {
    loadWizardProjectContext(wizardProjectId.value)
  }
  drawerOpen.value = true
}

async function detectHostFromHarFiles(files: File[]) {
  if (files.length === 0) {
    harDetectedOrigin.value = null
    return
  }
  try {
    const sampleChunk = await files[0].slice(0, 65536).text()
    const match = sampleChunk.match(/"url"\s*:\s*"(https?:\/\/[^"\\]+)"/i)
    if (match) {
      harDetectedOrigin.value = parseOriginFromText(match[1])
      return
    }
    harDetectedOrigin.value = parseOriginFromText(sampleChunk)
  } catch {
    harDetectedOrigin.value = null
  }
}

function addUploadFiles(fileList: FileList | null) {
  if (!fileList) return
  const seen = new Set(uploadFiles.value.map((f) => `${f.name}:${f.size}`))
  const next = [...uploadFiles.value]
  for (const file of Array.from(fileList)) {
    const key = `${file.name}:${file.size}`
    if (!seen.has(key)) {
      seen.add(key)
      next.push(file)
    }
  }
  uploadFiles.value = next
  detectHostFromHarFiles(next)
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  addUploadFiles(input.files)
  input.value = ''
}

function onDrop(event: DragEvent) {
  isDragging.value = false
  addUploadFiles(event.dataTransfer?.files ?? null)
}

function removeUploadFile(index: number) {
  const next = uploadFiles.value.filter((_, i) => i !== index)
  uploadFiles.value = next
  detectHostFromHarFiles(next)
}

async function copyMitmCommand() {
  try {
    await navigator.clipboard.writeText(MITM_HINT_CMD)
    copiedMitm.value = true
    window.setTimeout(() => {
      copiedMitm.value = false
    }, 1600)
  } catch {
    // ignore clipboard errors
  }
}

function loadSampleCurl() {
  pasteContent.value = SAMPLE_CURL_SNIPPET
  if (!pasteScenario.value.trim()) {
    pasteScenario.value = '01_devtools_sample'
  }
  pasteFormat.value = 'auto'
}

async function ensureProjectAndTarget(): Promise<{
  projectId: string
  targetId: string | null
  targetUrl: string | null
}> {
  if (projectMode.value === 'existing') {
    if (!wizardProjectId.value) {
      throw new Error(t('runs.wizard.errNoProject'))
    }
    const matchedTarget = wizardTargets.value.find((tItem) => tItem.id === wizardTargetId.value)
    return {
      projectId: wizardProjectId.value,
      targetId: wizardTargetId.value || null,
      targetUrl: matchedTarget?.base_url ?? detectedOrigin.value?.baseUrl ?? null,
    }
  }

  const desiredName =
    projectMode.value === 'new'
      ? inlineNewProjectName.value.trim()
      : autoResolvedProjectName.value.trim()

  if (!desiredName) {
    throw new Error(t('runs.wizard.errNewProjectName'))
  }

  let project = findMatchingProject(desiredName)
  let createdFresh = false
  if (!project) {
    submitStepLabel.value = t('runs.wizard.statusCreatingProject', { name: desiredName })
    project = await createProject({
      name: desiredName,
      description: t('runs.wizard.project.newDescDefault'),
    })
    projects.value = [project, ...projects.value]
    upsertProject(project)
    createdFresh = true
  }

  let resolvedTargetId: string | null = null
  const origin = detectedOrigin.value
  if (origin && createdFresh) {
    try {
      const createdTarget = await createTarget(project.id, {
        root_domain: origin.host,
        base_url: origin.baseUrl,
      })
      resolvedTargetId = createdTarget.id
    } catch {
      // non-fatal if target already exists or validation fails
    }
  }

  return {
    projectId: project.id,
    targetId: resolvedTargetId,
    targetUrl: origin?.baseUrl ?? null,
  }
}

async function handleLaunchRun() {
  wizardError.value = ''

  if (harInputMode.value === 'upload' && uploadFiles.value.length === 0) {
    wizardError.value = t('runs.wizard.errNoHar')
    return
  }
  if (harInputMode.value === 'paste' && !pasteContent.value.trim()) {
    wizardError.value = t('runs.wizard.errNoPaste')
    return
  }
  if (harInputMode.value === 'reuse' && !reuseSourceRunId.value) {
    wizardError.value = t('runs.wizard.errNoReuse')
    return
  }
  if (projectMode.value === 'new' && !inlineNewProjectName.value.trim()) {
    wizardError.value = t('runs.wizard.errNewProjectName')
    return
  }
  if (projectMode.value === 'existing' && !wizardProjectId.value) {
    wizardError.value = t('runs.wizard.errNoProject')
    return
  }

  submitting.value = true
  try {
    const { projectId, targetId, targetUrl } = await ensureProjectAndTarget()

    // Fast Core Analysis only (merge -> OpenAPI + Correlation + Coverage).
    // API Automation, Performance & Security are triggered on demand inside RunDetailView!
    const trimmedName = customRunName.value.trim()
    const payload = {
      name: trimmedName || undefined,
      target_id: targetId,
      target_url: targetUrl,
      modules: ['merge'],
      trigger_source: 'web' as const,
      config: trimmedName ? { run_name: trimmedName } : {},
    }

    submitStepLabel.value = t('runs.wizard.statusCreatingPending')
    const created = await createRun(projectId, payload, false)

    if (harInputMode.value === 'upload') {
      submitStepLabel.value = t('runs.wizard.statusUploading', { count: uploadFiles.value.length })
      await uploadRunHars(created.id, uploadFiles.value, {
        scenario:
          uploadFiles.value.length === 1 && uploadScenario.value.trim()
            ? uploadScenario.value.trim()
            : undefined,
      })
    } else if (harInputMode.value === 'paste') {
      submitStepLabel.value = t('runs.wizard.statusImportingText')
      await importRunText(created.id, {
        content: pasteContent.value,
        scenario: pasteScenario.value.trim() || '01_devtools',
        format: pasteFormat.value,
      })
    } else {
      submitStepLabel.value = t('runs.wizard.statusReusing')
      const scenarios = reuseScenariosText.value
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
      await reuseRunHars(
        created.id,
        reuseSourceRunId.value,
        scenarios.length > 0 ? scenarios : undefined,
      )
    }

    submitStepLabel.value = t('runs.wizard.statusStarting')
    await startRun(created.id)

    uploadFiles.value = []
    uploadScenario.value = ''
    harDetectedOrigin.value = null
    pasteContent.value = ''
    reuseScenariosText.value = ''
    inlineNewProjectName.value = ''
    customRunName.value = ''
    drawerOpen.value = false
    setActiveProject(projectId)
    await router.push(`/p/${projectId}/runs/${created.id}`)
  } catch (err) {
    wizardError.value =
      err instanceof Error ? err.message : t('runs.wizard.errLaunch')
  } finally {
    submitting.value = false
    submitStepLabel.value = ''
  }
}

function getRunModules(run: Run): string[] {
  const fromConfig = run.config_snapshot?.modules
  if (Array.isArray(fromConfig) && fromConfig.length > 0) {
    return fromConfig.map(String)
  }
  return run.jobs.map((j) => j.module)
}

function metricNumber(metrics: Record<string, unknown> | undefined, keys: string[]): number | null {
  if (!metrics) return null
  for (const k of keys) {
    const val = metrics[k]
    if (typeof val === 'number' && !Number.isNaN(val)) return val
  }
  return null
}

function formatDateTime(iso?: string | null): string {
  if (!iso) return t('runs.table.notStarted')
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(locale.value === 'zh' ? 'zh-CN' : 'en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

watch(routeProjectId, () => {
  void loadProjectsAndRuns()
})

onMounted(async () => {
  await ensureWorkspaceLoaded()
  await loadProjectsAndRuns()
  if (route.query.new === '1') {
    openWizard()
  }
})
</script>

<template>
  <div class="space-y-6">
    <!-- Top Header & Controls -->
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div>
        <h1 class="text-xl font-bold tracking-tight text-slate-900">
          {{ t('runs.title') }}
        </h1>
        <p class="mt-0.5 text-sm text-slate-500">
          {{ t('runs.subtitle') }}
        </p>
      </div>

      <div class="flex flex-wrap items-center gap-2.5">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 shadow-sm transition hover:bg-slate-50"
          @click="loadProjectsAndRuns"
        >
          <RefreshCw class="h-4 w-4" :class="loading ? 'animate-spin' : ''" />
          <span>{{ t('runs.refresh') }}</span>
        </button>
        <button
          type="button"
          data-testid="open-new-run-wizard"
          class="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700"
          @click="openWizard"
        >
          <Plus class="h-4 w-4" />
          <span>{{ t('runs.newRunBtn') }}</span>
        </button>
      </div>
    </div>

    <!-- Filter Bar -->
    <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div class="flex flex-wrap items-center gap-3">
        <div v-if="!scopedProjectId" class="flex items-center gap-2">
          <SlidersHorizontal class="h-4 w-4 text-slate-400" />
          <label class="text-xs font-medium text-slate-600">{{ t('runs.filter.project') }}</label>
          <select
            v-model="selectedProjectFilter"
            class="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
          >
            <option value="all">{{ t('runs.filter.allProjects', { count: projects.length }) }}</option>
            <option v-for="proj in projects" :key="proj.id" :value="proj.id">
              {{ proj.name }}
            </option>
          </select>
        </div>

        <div class="relative min-w-[200px] flex-1 sm:max-w-xs">
          <Search class="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
          <input
            v-model="runSearch"
            type="text"
            data-testid="runs-search-input"
            :placeholder="t('runs.filter.searchPlaceholder')"
            class="w-full rounded-lg border border-slate-300 bg-white py-1.5 pl-8 pr-3 text-xs text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
          />
        </div>

        <div class="flex items-center gap-1 rounded-lg bg-slate-100 p-1">
          <button
            v-for="st in STATUS_FILTER_KEYS"
            :key="st"
            type="button"
            class="rounded-md px-2.5 py-1 text-xs font-medium transition"
            :class="statusFilter === st ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600 hover:text-slate-900'"
            @click="statusFilter = st"
          >
            {{ t(`runs.filter.status.${st}`) }}
          </button>
        </div>
      </div>

      <span class="text-xs text-slate-500">
        {{ t('runs.filter.showing') }}
        <strong class="text-slate-800">{{ filteredRuns.length }}</strong>
        {{ t('runs.filter.runsUnit') }}
      </span>
    </div>

    <!-- Error Banner -->
    <div
      v-if="errorMessage"
      class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"
    >
      {{ errorMessage }}
    </div>

    <!-- Runs Table -->
    <div
      v-if="loading"
      class="flex items-center justify-center rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500"
    >
      {{ t('runs.loading') }}
    </div>

    <EmptyState
      v-else-if="projects.length === 0"
      :title="t('runs.emptyNoProjects.title')"
      :description="t('runs.emptyNoProjects.desc')"
    >
      <div class="mt-3 flex flex-wrap items-center justify-center gap-2.5">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          @click="openWizard"
        >
          <Plus class="h-4 w-4" />
          {{ t('runs.newRunBtn') }}
        </button>
        <router-link
          to="/projects"
          class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
        >
          {{ t('runs.emptyNoProjects.cta') }}
          <ArrowRight class="h-4 w-4" />
        </router-link>
      </div>
    </EmptyState>

    <EmptyState
      v-else-if="filteredRuns.length === 0"
      :title="t('runs.emptyNoRuns.title')"
      :description="t('runs.emptyNoRuns.desc')"
    >
      <button
        type="button"
        class="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
        @click="openWizard"
      >
        <Plus class="h-4 w-4" />
        {{ t('runs.newRunBtn') }}
      </button>
    </EmptyState>

    <div v-else class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div class="overflow-x-auto">
        <table class="min-w-full divide-y divide-slate-200 text-left text-sm">
          <thead class="bg-slate-50 text-xs font-semibold tracking-wider text-slate-500 uppercase">
            <tr>
              <th class="px-5 py-3.5">{{ t('runs.table.runId') }}</th>
              <th v-if="selectedProjectFilter === 'all'" class="px-4 py-3.5">{{ t('runs.table.project') }}</th>
              <th class="px-4 py-3.5">{{ t('runs.table.status') }}</th>
              <th class="px-4 py-3.5">{{ t('runs.table.trigger') }}</th>
              <th class="px-4 py-3.5">{{ t('runs.table.modules') }}</th>
              <th class="px-4 py-3.5 text-right">{{ t('runs.table.endpoints') }}</th>
              <th class="px-4 py-3.5 text-right">{{ t('runs.table.coverage') }}</th>
              <th class="px-4 py-3.5 text-right">{{ t('runs.table.findings') }}</th>
              <th class="px-4 py-3.5">{{ t('runs.table.started') }}</th>
              <th class="px-5 py-3.5 text-right">{{ t('runs.table.details') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr
              v-for="run in filteredRuns"
              :key="run.id"
              class="cursor-pointer transition hover:bg-slate-50/90"
              @click="router.push(`/p/${run.project_id}/runs/${run.id}`)"
            >
              <td class="px-5 py-3.5">
                <div
                  class="max-w-[260px] truncate text-sm font-semibold text-slate-900"
                  :title="getRunTitle(run, run.project_name)"
                >
                  {{ getRunTitle(run, run.project_name) }}
                </div>
                <div class="mt-0.5 flex items-center gap-1.5 font-mono text-[11px] text-slate-400">
                  <span :title="run.id">{{ shortRunId(run.id) }}</span>
                  <span
                    v-if="run.config_snapshot?.target_url"
                    class="max-w-[180px] truncate"
                    :title="String(run.config_snapshot.target_url)"
                  >
                    · {{ run.config_snapshot.target_url }}
                  </span>
                </div>
              </td>
              <td v-if="selectedProjectFilter === 'all'" class="px-4 py-3.5 text-xs font-medium text-slate-700">
                {{ run.project_name || projectMap.get(run.project_id)?.name || '—' }}
              </td>
              <td class="px-4 py-3.5">
                <StatusBadge :status="run.status" size="sm" />
              </td>
              <td class="px-4 py-3.5">
                <span class="rounded bg-slate-100 px-2 py-0.5 font-mono text-[11px] font-medium text-slate-600 uppercase">
                  {{ run.trigger_source }}
                </span>
              </td>
              <td class="px-4 py-3.5">
                <div class="flex flex-wrap gap-1">
                  <span
                    v-for="mod in getRunModules(run)"
                    :key="mod"
                    class="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[11px] text-slate-700"
                  >
                    {{ mod }}
                  </span>
                </div>
              </td>
              <td class="px-4 py-3.5 text-right font-mono text-xs font-semibold text-slate-800">
                {{ metricNumber(run.summary_metrics, ['total_endpoints', 'endpoints_count', 'endpoints']) ?? '—' }}
              </td>
              <td class="px-4 py-3.5 text-right font-mono text-xs">
                <span
                  v-if="metricNumber(run.summary_metrics, ['coverage_pct']) !== null"
                  class="font-semibold text-emerald-700"
                >
                  {{ metricNumber(run.summary_metrics, ['coverage_pct'])?.toFixed(1) }}%
                </span>
                <span v-else class="text-slate-400">—</span>
              </td>
              <td class="px-4 py-3.5 text-right font-mono text-xs">
                <span
                  v-if="metricNumber(run.summary_metrics, ['findings_total', 'findings_count', 'total_findings', 'security_findings_count']) !== null"
                  class="rounded-full bg-slate-100 px-2 py-0.5 font-semibold text-slate-800"
                >
                  {{ metricNumber(run.summary_metrics, ['findings_total', 'findings_count', 'total_findings', 'security_findings_count']) }}
                </span>
                <span v-else class="text-slate-400">—</span>
              </td>
              <td class="px-4 py-3.5 text-xs text-slate-500">
                {{ formatDateTime(run.started_at) }}
              </td>
              <td class="px-5 py-3.5 text-right" @click.stop>
                <div class="inline-flex items-center justify-end gap-2">
                  <template v-if="confirmDeleteRunId === run.id">
                    <button
                      type="button"
                      data-testid="delete-run-confirm"
                      :disabled="deletingRunId === run.id"
                      class="rounded bg-rose-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-rose-700 disabled:opacity-50"
                      @click="handleDeleteRun(run.id)"
                    >
                      {{ t('runs.table.deleteConfirm') }}
                    </button>
                    <button
                      type="button"
                      class="rounded bg-slate-100 px-2 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-200"
                      @click="confirmDeleteRunId = null"
                    >
                      {{ t('runs.table.deleteCancel') }}
                    </button>
                  </template>
                  <template v-else>
                    <router-link
                      :to="`/p/${run.project_id}/runs/${run.id}`"
                      class="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800"
                    >
                      {{ t('runs.table.open') }}
                      <ArrowRight class="h-3.5 w-3.5" />
                    </router-link>
                    <button
                      type="button"
                      data-testid="delete-run-btn"
                      :disabled="run.status === 'running'"
                      :title="
                        run.status === 'running'
                          ? t('runs.table.deleteDisabledRunning')
                          : t('runs.table.deleteTitle')
                      "
                      class="rounded p-1.5 text-slate-400 transition hover:bg-rose-50 hover:text-rose-600 disabled:cursor-not-allowed disabled:opacity-35"
                      @click="confirmDeleteRunId = run.id"
                    >
                      <Trash2 class="h-3.5 w-3.5" />
                    </button>
                  </template>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Clean 2-Step Input-First Slide-Over Wizard Drawer (Ingest & Merge First; Capabilities On Demand) -->
    <Teleport to="body">
      <div
        v-if="drawerOpen"
        class="fixed inset-0 z-50 flex justify-end bg-slate-950/50 backdrop-blur-sm"
        @click.self="drawerOpen = false"
      >
        <div class="flex h-full w-full max-w-2xl flex-col bg-white shadow-2xl">
          <!-- Drawer Header -->
          <div class="flex items-center justify-between border-b border-slate-200 px-6 py-4">
            <div>
              <h2 class="text-base font-bold text-slate-900">{{ t('runs.wizard.title') }}</h2>
              <p class="mt-0.5 text-xs text-slate-500">
                {{ t('runs.wizard.subtitle') }}
              </p>
            </div>
            <button
              type="button"
              class="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
              @click="drawerOpen = false"
            >
              <X class="h-5 w-5" />
            </button>
          </div>

          <!-- Drawer Body: Strictly 2 Minimal Steps -->
          <div class="flex-1 space-y-5 overflow-y-auto px-6 py-5">
            <div
              v-if="wizardError"
              class="rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-sm text-rose-700"
            >
              {{ wizardError }}
            </div>

            <!-- STEP 1: Traffic Input -->
            <section
              data-testid="wizard-step-1"
              class="space-y-3 rounded-xl border border-slate-200 bg-slate-50/50 p-4"
            >
              <h3 class="text-xs font-bold tracking-wider text-slate-800 uppercase">
                {{ t('runs.wizard.step1.title') }}
              </h3>

              <!-- 3 First-Class Input Tabs -->
              <div class="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <button
                  type="button"
                  data-testid="input-tab-upload"
                  class="flex flex-col items-start rounded-xl border p-3 text-left transition"
                  :class="
                    harInputMode === 'upload'
                      ? 'border-indigo-500 bg-white ring-2 ring-indigo-500/20'
                      : 'border-slate-200 bg-white/70 hover:bg-white'
                  "
                  @click="harInputMode = 'upload'"
                >
                  <FileUp class="mb-1.5 h-4 w-4 text-indigo-600" />
                  <span class="text-xs font-semibold text-slate-900">{{ t('runs.wizard.tab.upload.title') }}</span>
                  <span class="mt-0.5 text-[11px] text-slate-500">{{ t('runs.wizard.tab.upload.desc') }}</span>
                </button>

                <button
                  type="button"
                  data-testid="input-tab-paste"
                  class="flex flex-col items-start rounded-xl border p-3 text-left transition"
                  :class="
                    harInputMode === 'paste'
                      ? 'border-indigo-500 bg-white ring-2 ring-indigo-500/20'
                      : 'border-slate-200 bg-white/70 hover:bg-white'
                  "
                  @click="harInputMode = 'paste'"
                >
                  <Code2 class="mb-1.5 h-4 w-4 text-indigo-600" />
                  <span class="text-xs font-semibold text-slate-900">{{ t('runs.wizard.tab.paste.title') }}</span>
                  <span class="mt-0.5 text-[11px] text-slate-500">{{ t('runs.wizard.tab.paste.desc') }}</span>
                </button>

                <button
                  type="button"
                  data-testid="input-tab-reuse"
                  class="flex flex-col items-start rounded-xl border p-3 text-left transition"
                  :class="
                    harInputMode === 'reuse'
                      ? 'border-indigo-500 bg-white ring-2 ring-indigo-500/20'
                      : 'border-slate-200 bg-white/70 hover:bg-white'
                  "
                  @click="harInputMode = 'reuse'"
                >
                  <History class="mb-1.5 h-4 w-4 text-indigo-600" />
                  <span class="text-xs font-semibold text-slate-900">{{ t('runs.wizard.tab.reuse.title') }}</span>
                  <span class="mt-0.5 text-[11px] text-slate-500">{{ t('runs.wizard.tab.reuse.desc') }}</span>
                </button>
              </div>

              <!-- Paste cURL / fetch / Raw HTTP panel -->
              <div v-if="harInputMode === 'paste'" class="space-y-3 pt-1">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <label class="text-xs font-medium text-slate-700">
                    {{ t('runs.wizard.paste.label') }}
                  </label>
                  <button
                    type="button"
                    data-testid="load-sample-curl-btn"
                    class="inline-flex items-center gap-1 rounded-md border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[11px] font-semibold text-indigo-700 transition hover:bg-indigo-100"
                    @click="loadSampleCurl"
                  >
                    <Sparkles class="h-3 w-3" />
                    <span>{{ t('runs.wizard.paste.loadSample') }}</span>
                  </button>
                </div>

                <textarea
                  v-model="pasteContent"
                  data-testid="paste-traffic-textarea"
                  rows="7"
                  spellcheck="false"
                  :placeholder="t('runs.wizard.paste.placeholder')"
                  class="w-full rounded-xl border border-slate-300 bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-100 placeholder:text-slate-500 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                />

                <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label class="block text-xs font-medium text-slate-700">{{ t('runs.wizard.paste.scenario') }}</label>
                    <input
                      v-model="pasteScenario"
                      type="text"
                      placeholder="01_devtools"
                      class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                    />
                  </div>
                  <div>
                    <label class="block text-xs font-medium text-slate-700">{{ t('runs.wizard.paste.format') }}</label>
                    <select
                      v-model="pasteFormat"
                      class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                    >
                      <option value="auto">{{ t('runs.wizard.paste.formatAuto') }}</option>
                      <option value="curl">{{ t('runs.wizard.paste.formatCurl') }}</option>
                      <option value="fetch">{{ t('runs.wizard.paste.formatFetch') }}</option>
                      <option value="raw_http">{{ t('runs.wizard.paste.formatRawHttp') }}</option>
                      <option value="har_json">{{ t('runs.wizard.paste.formatHar') }}</option>
                    </select>
                  </div>
                </div>
              </div>

              <!-- Upload HAR panel -->
              <div v-else-if="harInputMode === 'upload'" class="space-y-3 pt-1">
                <label
                  class="flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-4 py-6 text-center transition"
                  :class="
                    isDragging
                      ? 'border-indigo-500 bg-indigo-50'
                      : 'border-slate-300 bg-white hover:border-indigo-400'
                  "
                  @dragover.prevent="isDragging = true"
                  @dragleave.prevent="isDragging = false"
                  @drop.prevent="onDrop"
                >
                  <UploadCloud class="mb-1.5 h-7 w-7 text-indigo-500" />
                  <span class="text-xs font-semibold text-slate-800">
                    {{ t('runs.wizard.upload.dropTitle') }}
                  </span>
                  <span class="mt-1 text-[11px] text-slate-500">
                    {{ t('runs.wizard.upload.dropHint') }}
                  </span>
                  <input
                    type="file"
                    accept=".har,application/json"
                    multiple
                    class="hidden"
                    @change="onFileChange"
                  />
                </label>

                <ul
                  v-if="uploadFiles.length > 0"
                  class="max-h-32 space-y-1.5 overflow-y-auto rounded-lg border border-slate-200 bg-white p-2"
                >
                  <li
                    v-for="(file, idx) in uploadFiles"
                    :key="`${file.name}-${idx}`"
                    class="flex items-center justify-between rounded bg-slate-50 px-2.5 py-1 text-xs"
                  >
                    <span class="truncate font-mono text-slate-800">{{ file.name }} ({{ formatBytes(file.size) }})</span>
                    <button
                      type="button"
                      class="ml-2 text-slate-400 hover:text-rose-600"
                      @click="removeUploadFile(idx)"
                    >
                      <Trash2 class="h-3.5 w-3.5" />
                    </button>
                  </li>
                </ul>

                <div>
                  <label class="block text-xs font-medium text-slate-700">
                    {{ t('runs.wizard.upload.scenario') }}
                    <span class="text-slate-400">{{ t('runs.wizard.upload.scenarioHint') }}</span>
                  </label>
                  <input
                    v-model="uploadScenario"
                    type="text"
                    :disabled="uploadFiles.length > 1"
                    :placeholder="t('runs.wizard.upload.scenarioPlaceholder')"
                    class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 disabled:bg-slate-100"
                  />
                </div>

                <!-- Copyable mitmproxy smoke-capture helper -->
                <div class="flex items-center justify-between gap-2 rounded-lg border border-slate-200 bg-slate-900 px-3 py-2 text-[11px] text-slate-200">
                  <div class="flex min-w-0 items-center gap-2">
                    <Terminal class="h-3.5 w-3.5 shrink-0 text-indigo-400" />
                    <span class="truncate font-mono" :title="MITM_HINT_CMD">
                      <span class="text-slate-400">{{ t('runs.wizard.mitmLabel') }}</span> {{ MITM_HINT_CMD }}
                    </span>
                  </div>
                  <button
                    type="button"
                    class="inline-flex shrink-0 items-center gap-1 rounded bg-slate-800 px-2 py-0.5 text-[10px] font-medium text-slate-200 hover:bg-slate-700"
                    @click="copyMitmCommand"
                  >
                    <Check v-if="copiedMitm" class="h-3 w-3 text-emerald-400" />
                    <Copy v-else class="h-3 w-3" />
                    <span>{{ copiedMitm ? t('runs.wizard.copied') : t('runs.wizard.copy') }}</span>
                  </button>
                </div>
              </div>

              <!-- Reuse panel -->
              <div v-else class="space-y-3 pt-1">
                <div
                  v-if="projects.length === 0"
                  class="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800"
                >
                  {{ t('runs.wizard.reuse.empty') }}
                </div>
                <template v-else>
                  <div>
                    <label class="block text-xs font-medium text-slate-700">{{ t('runs.wizard.project.reusePickProject') }}</label>
                    <select
                      v-model="wizardProjectId"
                      class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none"
                    >
                      <option v-for="p in projects" :key="p.id" :value="p.id">
                        {{ p.name }}
                      </option>
                    </select>
                  </div>
                  <div
                    v-if="wizardProjectRuns.length === 0"
                    class="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800"
                  >
                    {{ t('runs.wizard.reuse.empty') }}
                  </div>
                  <template v-else>
                    <div>
                      <label class="block text-xs font-medium text-slate-700">{{ t('runs.wizard.reuse.sourceRun') }}</label>
                      <select
                        v-model="reuseSourceRunId"
                        class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none"
                      >
                        <option v-for="r in wizardProjectRuns" :key="r.id" :value="r.id">
                          {{ getRunTitle(r) }} ({{ shortRunId(r.id) }}) · {{ r.status }} · {{ formatDateTime(r.started_at) }}
                        </option>
                      </select>
                    </div>
                    <div>
                      <label class="block text-xs font-medium text-slate-700">
                        {{ t('runs.wizard.reuse.scenarios') }}
                        <span class="text-slate-400">{{ t('runs.wizard.reuse.scenariosHint') }}</span>
                      </label>
                      <input
                        v-model="reuseScenariosText"
                        type="text"
                        :placeholder="t('runs.wizard.reuse.scenariosPlaceholder')"
                        class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none"
                      />
                    </div>
                  </template>
                </template>
              </div>

              <!-- Live Detected Host Preview Pill -->
              <div
                v-if="detectedOrigin"
                data-testid="detected-host-pill"
                class="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50/80 px-3 py-2 text-xs text-emerald-800"
              >
                <Globe class="h-3.5 w-3.5 shrink-0 text-emerald-600" />
                <span class="font-medium">
                  {{ t('runs.wizard.detectedHost', { host: detectedOrigin.host }) }}
                </span>
                <span class="ml-auto font-mono text-[11px] text-emerald-700/80">
                  {{ detectedOrigin.baseUrl }}
                </span>
              </div>

              <!-- Subtle 1-line link to dedicated /explore workspace -->
              <div class="flex items-center justify-between pt-0.5 text-[11px] text-slate-500">
                <span class="inline-flex items-center gap-1.5">
                  <Compass class="h-3.5 w-3.5 text-slate-400" />
                  <span>{{ t('runs.wizard.exploreHint') }}</span>
                </span>
                <router-link
                  :to="scopedProjectId ? `/p/${scopedProjectId}/explore` : '/explore'"
                  class="font-medium text-indigo-600 hover:underline"
                  @click="drawerOpen = false"
                >
                  {{ t('runs.wizard.exploreLink') }}
                </router-link>
              </div>
            </section>

            <!-- STEP 2: Flexible Project Assignment -->
            <section
              data-testid="wizard-step-2"
              class="space-y-3 rounded-xl border border-slate-200 bg-white p-4"
            >
              <h3 class="text-xs font-bold tracking-wider text-slate-800 uppercase">
                {{ t('runs.wizard.project.stepTitle') }}
              </h3>

              <!-- 3 Mode Pills -->
              <div class="grid grid-cols-3 gap-1.5 rounded-lg bg-slate-100 p-1">
                <button
                  type="button"
                  data-testid="project-mode-auto"
                  :disabled="harInputMode === 'reuse'"
                  class="rounded-md px-2.5 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-40"
                  :class="
                    projectMode === 'auto'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="projectMode = 'auto'"
                >
                  {{ t('runs.wizard.project.modeAuto') }}
                </button>

                <button
                  type="button"
                  data-testid="project-mode-new"
                  :disabled="harInputMode === 'reuse'"
                  class="rounded-md px-2.5 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-40"
                  :class="
                    projectMode === 'new'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="projectMode = 'new'"
                >
                  {{ t('runs.wizard.project.modeNew') }}
                </button>

                <button
                  type="button"
                  data-testid="project-mode-existing"
                  class="rounded-md px-2.5 py-1.5 text-xs font-medium transition"
                  :class="
                    projectMode === 'existing'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="projectMode = 'existing'"
                >
                  {{ t('runs.wizard.project.modeExisting') }}
                </button>
              </div>

              <!-- Auto Mode Preview -->
              <div
                v-if="projectMode === 'auto'"
                data-testid="auto-project-preview"
                class="flex flex-wrap items-center gap-2 rounded-lg border border-indigo-200/80 bg-indigo-50/50 px-3.5 py-2.5 text-xs text-indigo-900"
              >
                <Sparkles class="h-3.5 w-3.5 shrink-0 text-indigo-600" />
                <span v-if="autoMatchedProject">
                  {{ t('runs.wizard.project.autoReuseHint', { name: autoMatchedProject.name }) }}
                </span>
                <span v-else>
                  {{ t('runs.wizard.project.autoCreateHint', { name: autoResolvedProjectName }) }}
                  <span v-if="detectedOrigin" class="text-indigo-700/80">
                    · {{ t('runs.wizard.project.autoHostTargetHint', { url: detectedOrigin.baseUrl }) }}
                  </span>
                </span>
              </div>

              <!-- Inline New Project Input -->
              <div v-else-if="projectMode === 'new'" class="space-y-1.5">
                <label class="flex items-center gap-1.5 text-xs font-medium text-slate-700">
                  <FolderPlus class="h-3.5 w-3.5 text-indigo-500" />
                  {{ t('runs.wizard.project.newNameLabel') }}
                </label>
                <input
                  v-model="inlineNewProjectName"
                  data-testid="inline-new-project-input"
                  type="text"
                  :placeholder="t('runs.wizard.project.newNamePlaceholder')"
                  class="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                />
              </div>

              <!-- Existing Project & Target Dropdowns -->
              <div v-else class="space-y-3">
                <div
                  v-if="projects.length === 0"
                  class="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-800"
                >
                  {{ t('runs.wizard.project.noExisting') }}
                </div>
                <div v-else class="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div>
                    <label class="block text-xs font-medium text-slate-700">{{ t('runs.wizard.projectLabel') }}</label>
                    <select
                      v-model="wizardProjectId"
                      class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                    >
                      <option v-for="p in projects" :key="p.id" :value="p.id">
                        {{ p.name }}
                      </option>
                    </select>
                  </div>
                  <div>
                    <label class="block text-xs font-medium text-slate-700">
                      {{ t('runs.wizard.targetLabel') }}
                      <span class="font-normal text-slate-400">{{ t('runs.wizard.optional') }}</span>
                    </label>
                    <select
                      v-model="wizardTargetId"
                      :disabled="loadingProjectContext"
                      class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 disabled:bg-slate-50"
                    >
                      <option value="">{{ t('runs.wizard.targetAuto') }}</option>
                      <option v-for="targetItem in wizardTargets" :key="targetItem.id" :value="targetItem.id">
                        {{ targetItem.root_domain }} ({{ targetItem.base_url }})
                      </option>
                    </select>
                  </div>
                </div>
              </div>
              <!-- Optional Custom Run Name -->
              <div class="border-t border-slate-100 pt-3">
                <label class="block text-xs font-medium text-slate-700">
                  {{ t('runs.wizard.runNameLabel') }}
                  <span class="font-normal text-slate-400">{{ t('runs.wizard.optional') }}</span>
                </label>
                <input
                  v-model="customRunName"
                  data-testid="wizard-run-name-input"
                  type="text"
                  :placeholder="t('runs.wizard.runNamePlaceholder')"
                  class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                />
              </div>
            </section>
            <div
              data-testid="wizard-deferred-note"
              class="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3.5 py-2.5 text-xs text-slate-600"
            >
              <Info class="mt-0.5 h-3.5 w-3.5 shrink-0 text-indigo-500" />
              <span>{{ t('runs.wizard.deferredNote') }}</span>
            </div>
          </div>

          <!-- Drawer Footer -->
          <div class="flex items-center justify-between border-t border-slate-200 bg-slate-50 px-6 py-4">
            <span class="text-xs text-slate-500">{{ submitStepLabel }}</span>
            <div class="flex items-center gap-2.5">
              <button
                type="button"
                :disabled="submitting"
                class="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                @click="drawerOpen = false"
              >
                {{ t('runs.wizard.cancel') }}
              </button>
              <button
                type="button"
                data-testid="launch-analysis-btn"
                :disabled="submitting || (harInputMode === 'reuse' && wizardProjectRuns.length === 0)"
                class="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-5 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                @click="handleLaunchRun"
              >
                <RefreshCw v-if="submitting" class="h-4 w-4 animate-spin" />
                <Play v-else class="h-4 w-4" />
                <span>{{ t('runs.wizard.submit') }}</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
