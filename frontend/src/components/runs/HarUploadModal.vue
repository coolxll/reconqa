<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  Code2,
  FileUp,
  History,
  Play,
  RefreshCw,
  Sparkles,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-vue-next'
import type { Run } from '@/api/types'
import { importRunText, reuseRunHars, startRun, uploadRunHars } from '@/api/runs'
import '@/i18n/locales/runs'
import { useI18n } from '@/i18n'

const { t } = useI18n()

const props = withDefaults(
  defineProps<{
    open: boolean
    runId: string
    projectId: string
    previousRuns?: Run[]
  }>(),
  {
    previousRuns: () => [],
  },
)

const emit = defineEmits<{
  close: []
  uploaded: [payload: Record<string, unknown>]
  started: [run: Run]
}>()

const activeMode = ref<'upload' | 'paste' | 'reuse'>('upload')
const selectedFiles = ref<File[]>([])
const scenarioName = ref('')
const targetDomain = ref('')
const pasteContent = ref('')
const pasteScenario = ref('01_devtools')
const sourceRunId = ref('')
const reuseScenariosText = ref('')
const autoStartAfter = ref(true)
const isDragging = ref(false)
const submitting = ref(false)
const errorMessage = ref('')
const statusNote = ref('')

const SAMPLE_CURL = `curl 'https://api.example.com/api/v1/products?category=electronics' \\
  -H 'Accept: application/json' \\
  -H 'Authorization: Bearer example-account-a-token'`

const reusableRuns = computed(() =>
  props.previousRuns.filter((r) => r.id !== props.runId),
)

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      errorMessage.value = ''
      statusNote.value = ''
      if (!sourceRunId.value && reusableRuns.value.length > 0) {
        sourceRunId.value = reusableRuns.value[0].id
      }
    }
  },
)

function addFiles(fileList: FileList | null) {
  if (!fileList) return
  const existing = new Set(selectedFiles.value.map((f) => `${f.name}:${f.size}`))
  const next = [...selectedFiles.value]
  for (const file of Array.from(fileList)) {
    const key = `${file.name}:${file.size}`
    if (!existing.has(key)) {
      existing.add(key)
      next.push(file)
    }
  }
  selectedFiles.value = next
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  addFiles(input.files)
  input.value = ''
}

function onDrop(event: DragEvent) {
  isDragging.value = false
  addFiles(event.dataTransfer?.files ?? null)
}

function removeFile(index: number) {
  selectedFiles.value = selectedFiles.value.filter((_, idx) => idx !== index)
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

function shortId(id: string): string {
  return id.length > 10 ? id.slice(0, 10) : id
}

async function handleSubmit() {
  errorMessage.value = ''
  statusNote.value = ''

  if (activeMode.value === 'upload' && selectedFiles.value.length === 0) {
    errorMessage.value = t('harModal.errNoFiles')
    return
  }
  if (activeMode.value === 'paste' && !pasteContent.value.trim()) {
    errorMessage.value = t('harModal.errNoPaste')
    return
  }
  if (activeMode.value === 'reuse' && !sourceRunId.value) {
    errorMessage.value = t('harModal.errNoReuse')
    return
  }

  submitting.value = true
  try {
    let result: Record<string, unknown>
    if (activeMode.value === 'upload') {
      statusNote.value = t('harModal.statusUploading', { count: selectedFiles.value.length })
      result = (await uploadRunHars(props.runId, selectedFiles.value, {
        scenario: selectedFiles.value.length === 1 && scenarioName.value.trim()
          ? scenarioName.value.trim()
          : undefined,
        targetDomain: targetDomain.value.trim() || undefined,
      })) as unknown as Record<string, unknown>
    } else if (activeMode.value === 'paste') {
      statusNote.value = t('harModal.statusImporting')
      result = (await importRunText(props.runId, {
        content: pasteContent.value,
        scenario: pasteScenario.value.trim() || '01_devtools',
        format: 'auto',
        target_domain: targetDomain.value.trim() || null,
      })) as unknown as Record<string, unknown>
    } else {
      statusNote.value = t('harModal.statusReusing')
      const scenarios = reuseScenariosText.value
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
      result = (await reuseRunHars(
        props.runId,
        sourceRunId.value,
        scenarios.length > 0 ? scenarios : undefined,
      )) as unknown as Record<string, unknown>
    }

    emit('uploaded', result)

    if (autoStartAfter.value) {
      statusNote.value = t('harModal.statusStarting')
      const startedRun = await startRun(props.runId)
      emit('started', startedRun)
    }

    selectedFiles.value = []
    scenarioName.value = ''
    targetDomain.value = ''
    pasteContent.value = ''
    reuseScenariosText.value = ''
    emit('close')
  } catch (err) {
    errorMessage.value =
      err instanceof Error ? err.message : t('harModal.errFailed')
  } finally {
    submitting.value = false
    statusNote.value = ''
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4 backdrop-blur-sm"
      @click.self="emit('close')"
    >
      <div class="w-full max-w-xl overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl">
        <!-- Header -->
        <div class="flex items-center justify-between border-b border-slate-200 px-6 py-4">
          <div>
            <h3 class="text-base font-semibold text-slate-900">
              {{ t('harModal.title') }}
            </h3>
            <p class="mt-0.5 text-xs text-slate-500">
              {{ t('harModal.subtitle', { runId: shortId(runId) }) }}
            </p>
          </div>
          <button
            type="button"
            class="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
            @click="emit('close')"
          >
            <X class="h-4 w-4" />
          </button>
        </div>

        <!-- Mode Tabs -->
        <div class="border-b border-slate-200 bg-slate-50 px-6 pt-3">
          <div class="flex gap-2">
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-t-lg border-b-2 px-3.5 py-2 text-xs font-medium transition"
              :class="
                activeMode === 'upload'
                  ? 'border-indigo-600 bg-white text-indigo-700'
                  : 'border-transparent text-slate-600 hover:text-slate-900'
              "
              @click="activeMode = 'upload'"
            >
              <FileUp class="h-3.5 w-3.5" />
              {{ t('harModal.tab.upload') }}
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-t-lg border-b-2 px-3.5 py-2 text-xs font-medium transition"
              :class="
                activeMode === 'paste'
                  ? 'border-indigo-600 bg-white text-indigo-700'
                  : 'border-transparent text-slate-600 hover:text-slate-900'
              "
              @click="activeMode = 'paste'"
            >
              <Code2 class="h-3.5 w-3.5" />
              {{ t('harModal.tab.paste') }}
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-t-lg border-b-2 px-3.5 py-2 text-xs font-medium transition"
              :class="
                activeMode === 'reuse'
                  ? 'border-indigo-600 bg-white text-indigo-700'
                  : 'border-transparent text-slate-600 hover:text-slate-900'
              "
              @click="activeMode = 'reuse'"
            >
              <History class="h-3.5 w-3.5" />
              {{ t('harModal.tab.reuse') }}
            </button>
          </div>
        </div>

        <!-- Body -->
        <div class="space-y-4 px-6 py-5">
          <div
            v-if="errorMessage"
            class="rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-sm text-rose-700"
          >
            {{ errorMessage }}
          </div>

          <!-- Upload Mode -->
          <div v-if="activeMode === 'upload'" class="space-y-4">
            <label
              class="flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-7 text-center transition"
              :class="
                isDragging
                  ? 'border-indigo-500 bg-indigo-50/70'
                  : 'border-slate-300 bg-slate-50/60 hover:border-indigo-400 hover:bg-slate-50'
              "
              @dragover.prevent="isDragging = true"
              @dragleave.prevent="isDragging = false"
              @drop.prevent="onDrop"
            >
              <UploadCloud class="mb-2 h-8 w-8 text-indigo-500" />
              <span class="text-sm font-medium text-slate-800">
                {{ t('harModal.dropTitle') }}
              </span>
              <span class="mt-1 text-xs text-slate-500">
                {{ t('harModal.dropHint') }}
              </span>
              <input
                type="file"
                accept=".har,application/json"
                multiple
                class="hidden"
                @change="onFileChange"
              />
            </label>

            <div v-if="selectedFiles.length > 0" class="space-y-2">
              <div class="flex items-center justify-between text-xs font-medium text-slate-600">
                <span>{{ t('harModal.selectedFiles', { count: selectedFiles.length }) }}</span>
                <button
                  type="button"
                  class="text-rose-600 hover:underline"
                  @click="selectedFiles = []"
                >
                  {{ t('harModal.clearAll') }}
                </button>
              </div>
              <ul class="max-h-36 space-y-1.5 overflow-y-auto rounded-lg border border-slate-200 bg-slate-50 p-2">
                <li
                  v-for="(file, idx) in selectedFiles"
                  :key="`${file.name}-${idx}`"
                  class="flex items-center justify-between rounded-md bg-white px-3 py-1.5 text-xs shadow-sm"
                >
                  <div class="flex min-w-0 items-center gap-2">
                    <span class="truncate font-mono font-medium text-slate-800">{{ file.name }}</span>
                    <span class="shrink-0 text-slate-400">{{ formatBytes(file.size) }}</span>
                  </div>
                  <button
                    type="button"
                    class="ml-2 rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                    @click="removeFile(idx)"
                  >
                    <Trash2 class="h-3.5 w-3.5" />
                  </button>
                </li>
              </ul>
            </div>

            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <label class="block text-xs font-medium text-slate-700">
                  {{ t('harModal.scenarioLabel') }}
                  <span class="text-slate-400">{{ t('harModal.scenarioSingleHint') }}</span>
                </label>
                <input
                  v-model="scenarioName"
                  type="text"
                  :disabled="selectedFiles.length > 1"
                  :placeholder="t('runs.wizard.upload.scenarioPlaceholder')"
                  class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 disabled:bg-slate-100 disabled:text-slate-400"
                />
              </div>
              <div>
                <label class="block text-xs font-medium text-slate-700">
                  {{ t('harModal.targetDomainLabel') }}
                  <span class="text-slate-400">{{ t('harModal.optionalHint') }}</span>
                </label>
                <input
                  v-model="targetDomain"
                  type="text"
                  placeholder="api.example.com"
                  class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                />
              </div>
            </div>
          </div>

          <!-- Paste Mode -->
          <div v-else-if="activeMode === 'paste'" class="space-y-3">
            <div class="flex items-center justify-between">
              <label class="text-xs font-medium text-slate-700">
                {{ t('harModal.pasteLabel') }}
              </label>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded bg-indigo-50 px-2 py-0.5 text-[11px] font-semibold text-indigo-700 hover:bg-indigo-100"
                @click="pasteContent = SAMPLE_CURL"
              >
                <Sparkles class="h-3 w-3" />
                {{ t('harModal.loadSample') }}
              </button>
            </div>
            <textarea
              v-model="pasteContent"
              rows="6"
              spellcheck="false"
              placeholder="curl 'https://api.example.com/api/v1/orders' -H 'Accept: application/json' ..."
              class="w-full rounded-lg border border-slate-300 bg-slate-950 p-3 font-mono text-xs text-slate-100 placeholder:text-slate-500 focus:border-indigo-500 focus:outline-none"
            />
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <label class="block text-xs font-medium text-slate-700">
                  {{ t('harModal.scenarioLabel') }}
                </label>
                <input
                  v-model="pasteScenario"
                  type="text"
                  placeholder="01_devtools"
                  class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none"
                />
              </div>
              <div>
                <label class="block text-xs font-medium text-slate-700">
                  {{ t('harModal.targetDomainLabel') }}
                </label>
                <input
                  v-model="targetDomain"
                  type="text"
                  placeholder="api.example.com"
                  class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none"
                />
              </div>
            </div>
          </div>

          <!-- Reuse Mode -->
          <div v-else class="space-y-4">
            <div v-if="reusableRuns.length === 0" class="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
              {{ t('harModal.reuseEmpty') }}
            </div>
            <template v-else>
              <div>
                <label class="block text-xs font-medium text-slate-700">
                  {{ t('harModal.reuseSource') }}
                </label>
                <select
                  v-model="sourceRunId"
                  class="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                >
                  <option
                    v-for="r in reusableRuns"
                    :key="r.id"
                    :value="r.id"
                  >
                    {{ shortId(r.id) }} — {{ r.status }} ({{ t('harModal.reuseEndpoints', { count: r.summary_metrics?.total_endpoints ?? '?' }) }})
                  </option>
                </select>
              </div>
              <div>
                <label class="block text-xs font-medium text-slate-700">
                  {{ t('harModal.reuseScenarios') }}
                  <span class="text-slate-400">{{ t('harModal.reuseScenariosHint') }}</span>
                </label>
                <input
                  v-model="reuseScenariosText"
                  type="text"
                  :placeholder="t('runs.wizard.reuse.scenariosPlaceholder')"
                  class="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
                />
                <p class="mt-1 text-xs text-slate-500">
                  {{ t('harModal.reuseHelp') }}
                </p>
              </div>
            </template>
          </div>

          <!-- Auto-start checkbox -->
          <label class="flex items-center gap-2.5 rounded-lg border border-slate-200 bg-slate-50 px-3.5 py-2.5 text-sm text-slate-700">
            <input
              v-model="autoStartAfter"
              type="checkbox"
              class="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
            />
            <span>{{ t('harModal.autoStart') }}</span>
          </label>
        </div>

        <!-- Footer -->
        <div class="flex items-center justify-between border-t border-slate-200 bg-slate-50 px-6 py-3.5">
          <span class="text-xs text-slate-500">{{ statusNote }}</span>
          <div class="flex items-center gap-2.5">
            <button
              type="button"
              :disabled="submitting"
              class="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:opacity-50"
              @click="emit('close')"
            >
              {{ t('harModal.cancel') }}
            </button>
            <button
              type="button"
              :disabled="submitting || (activeMode === 'reuse' && reusableRuns.length === 0)"
              class="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
              @click="handleSubmit"
            >
              <RefreshCw v-if="submitting" class="h-4 w-4 animate-spin" />
              <Play v-else class="h-4 w-4" />
              <span>
                {{
                  activeMode === 'upload'
                    ? autoStartAfter
                      ? t('harModal.btnUploadStart')
                      : t('harModal.btnUpload')
                    : activeMode === 'paste'
                      ? autoStartAfter
                        ? t('harModal.btnPasteStart')
                        : t('harModal.btnPaste')
                      : autoStartAfter
                        ? t('harModal.btnReuseStart')
                        : t('harModal.btnReuse')
                }}
              </span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
