<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Check,
  Copy,
  RefreshCw,
  Terminal,
  Trash2,
} from 'lucide-vue-next'
import { runEventsUrl } from '@/api/client'
import '@/i18n/locales/runs'
import { useI18n } from '@/i18n'

const { t } = useI18n()

interface LogEntry {
  id: number
  timestamp: string
  event: string
  message: string
  level: 'info' | 'success' | 'warn' | 'error' | 'system'
}

const props = defineProps<{
  runId: string
  status?: string
}>()

const emit = defineEmits<{
  'status-change': [status: string]
}>()

const logs = ref<LogEntry[]>([])
const connectionState = ref<'connecting' | 'connected' | 'completed' | 'failed' | 'disconnected'>('connecting')
const autoScroll = ref(true)
const copied = ref(false)
const scrollContainer = ref<HTMLElement | null>(null)

let eventSource: EventSource | null = null
let seq = 0

const stateMeta = computed(() => {
  switch (connectionState.value) {
    case 'connected':
      return {
        label: t('sse.state.connected'),
        dotClass: 'bg-emerald-400 animate-pulse',
        badgeClass: 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300',
      }
    case 'connecting':
      return {
        label: t('sse.state.connecting'),
        dotClass: 'bg-amber-400 animate-pulse',
        badgeClass: 'border-amber-500/30 bg-amber-500/15 text-amber-300',
      }
    case 'completed':
      return {
        label: t('sse.state.completed'),
        dotClass: 'bg-sky-400',
        badgeClass: 'border-sky-500/30 bg-sky-500/15 text-sky-300',
      }
    case 'failed':
      return {
        label: t('sse.state.failed'),
        dotClass: 'bg-rose-400',
        badgeClass: 'border-rose-500/30 bg-rose-500/15 text-rose-300',
      }
    default:
      return {
        label: t('sse.state.disconnected'),
        dotClass: 'bg-slate-400',
        badgeClass: 'border-slate-600 bg-slate-800 text-slate-300',
      }
  }
})

function formatTime(iso?: string): string {
  const date = iso ? new Date(iso) : new Date()
  if (Number.isNaN(date.getTime())) {
    return new Date().toTimeString().slice(0, 8)
  }
  return date.toTimeString().slice(0, 8)
}

function classifyMessage(eventName: string, message: string): LogEntry['level'] {
  if (eventName === 'run_failed' || eventName === 'job_failed') return 'error'
  if (eventName === 'run_completed' || eventName === 'job_completed') return 'success'
  if (eventName === 'snapshot' || eventName === 'run_queued' || eventName === 'run_started' || eventName === 'job_running') {
    return 'system'
  }
  const lower = message.toLowerCase()
  if (lower.includes('error') || lower.includes('failed') || lower.includes('traceback')) {
    return 'error'
  }
  if (lower.includes('warn') || lower.includes('skip')) {
    return 'warn'
  }
  if (lower.includes('completed') || lower.includes('generated') || lower.includes('merged')) {
    return 'success'
  }
  return 'info'
}

function appendLog(eventName: string, message: string, timestamp?: string) {
  seq += 1
  logs.value.push({
    id: seq,
    timestamp: formatTime(timestamp),
    event: eventName,
    message,
    level: classifyMessage(eventName, message),
  })
  if (logs.value.length > 1500) {
    logs.value.splice(0, logs.value.length - 1500)
  }
  if (autoScroll.value) {
    nextTick(() => {
      if (scrollContainer.value) {
        scrollContainer.value.scrollTop = scrollContainer.value.scrollHeight
      }
    })
  }
}

function closeStream() {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}

function parsePayload(raw: string): Record<string, unknown> {
  try {
    return JSON.parse(raw) as Record<string, unknown>
  } catch {
    return { message: raw }
  }
}

function connectStream() {
  closeStream()
  if (!props.runId) return

  connectionState.value = 'connecting'
  const url = runEventsUrl(props.runId)
  const es = new EventSource(url)
  eventSource = es

  es.onopen = () => {
    if (connectionState.value === 'connecting') {
      connectionState.value = 'connected'
    }
  }

  es.addEventListener('snapshot', (ev: MessageEvent) => {
    const data = parsePayload(ev.data)
    const st = String(data.status ?? 'unknown')
    const jobs = Array.isArray(data.jobs) ? data.jobs : []
    const jobSummary = jobs
      .map((j) => `${(j as Record<string, unknown>).module}:${(j as Record<string, unknown>).status}`)
      .join(', ')
    appendLog(
      'snapshot',
      `Run snapshot loaded (status=${st}${jobSummary ? `, jobs=[${jobSummary}]` : ''})`,
      typeof data.timestamp === 'string' ? data.timestamp : undefined,
    )
    if (st === 'completed') {
      connectionState.value = 'completed'
    } else if (st === 'failed') {
      connectionState.value = 'failed'
    } else {
      connectionState.value = 'connected'
    }
  })

  es.addEventListener('run_queued', (ev: MessageEvent) => {
    const data = parsePayload(ev.data)
    appendLog('run_queued', 'Run enqueued in pipeline worker.', String(data.timestamp ?? ''))
    emit('status-change', 'queued')
  })

  es.addEventListener('run_started', (ev: MessageEvent) => {
    const data = parsePayload(ev.data)
    const mods = Array.isArray(data.modules) ? data.modules.join(' -> ') : ''
    appendLog(
      'run_started',
      `Pipeline execution started${mods ? ` [${mods}]` : ''}.`,
      String(data.timestamp ?? ''),
    )
    emit('status-change', 'running')
  })

  es.addEventListener('log', (ev: MessageEvent) => {
    const data = parsePayload(ev.data)
    appendLog(
      'log',
      String(data.message ?? ''),
      typeof data.timestamp === 'string' ? data.timestamp : undefined,
    )
  })

  for (const jobEv of ['job_running', 'job_completed', 'job_failed', 'job_skipped']) {
    es.addEventListener(jobEv, (ev: MessageEvent) => {
      const data = parsePayload(ev.data)
      const mod = String(data.module ?? 'module')
      const st = String(data.status ?? jobEv.replace('job_', ''))
      appendLog(
        jobEv,
        `Module [${mod}] -> ${st}`,
        typeof data.timestamp === 'string' ? data.timestamp : undefined,
      )
      emit('status-change', st)
    })
  }

  es.addEventListener('run_completed', (ev: MessageEvent) => {
    const data = parsePayload(ev.data)
    const metrics = data.summary_metrics as Record<string, unknown> | undefined
    const endpoints = metrics?.total_endpoints ?? metrics?.endpoints
    const cov = metrics?.coverage_pct
    const extra = [
      endpoints !== undefined ? `endpoints=${endpoints}` : '',
      cov !== undefined ? `coverage=${cov}%` : '',
    ]
      .filter(Boolean)
      .join(', ')
    appendLog(
      'run_completed',
      `Pipeline completed successfully${extra ? ` (${extra})` : ''}.`,
      typeof data.timestamp === 'string' ? data.timestamp : undefined,
    )
    connectionState.value = 'completed'
    emit('status-change', 'completed')
    closeStream()
  })

  es.addEventListener('run_failed', (ev: MessageEvent) => {
    const data = parsePayload(ev.data)
    const errText = data.error ? `: ${data.error}` : ''
    appendLog(
      'run_failed',
      `Pipeline failed${errText}`,
      typeof data.timestamp === 'string' ? data.timestamp : undefined,
    )
    connectionState.value = 'failed'
    emit('status-change', 'failed')
    closeStream()
  })

  es.onmessage = (ev: MessageEvent) => {
    const data = parsePayload(ev.data)
    appendLog(
      String(data.event ?? 'message'),
      String(data.message ?? ev.data),
      typeof data.timestamp === 'string' ? data.timestamp : undefined,
    )
  }

  es.onerror = () => {
    if (connectionState.value === 'completed' || connectionState.value === 'failed') {
      closeStream()
      return
    }
    if (props.status === 'completed') {
      connectionState.value = 'completed'
      closeStream()
    } else if (props.status === 'failed') {
      connectionState.value = 'failed'
      closeStream()
    } else {
      connectionState.value = 'disconnected'
    }
  }
}

function clearLogs() {
  logs.value = []
}

async function copyLogs() {
  const text = logs.value
    .map((entry) => `[${entry.timestamp}] [${entry.event}] ${entry.message}`)
    .join('\n')
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    window.setTimeout(() => {
      copied.value = false
    }, 1800)
  } catch {
    // ignore clipboard failures in restricted contexts
  }
}

function levelTextClass(level: LogEntry['level']): string {
  switch (level) {
    case 'success':
      return 'text-emerald-300'
    case 'warn':
      return 'text-amber-300'
    case 'error':
      return 'text-rose-400'
    case 'system':
      return 'text-sky-300'
    default:
      return 'text-slate-200'
  }
}

function eventPillClass(level: LogEntry['level']): string {
  switch (level) {
    case 'success':
      return 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30'
    case 'warn':
      return 'bg-amber-500/15 text-amber-300 border-amber-500/30'
    case 'error':
      return 'bg-rose-500/15 text-rose-300 border-rose-500/30'
    case 'system':
      return 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30'
    default:
      return 'bg-slate-800 text-slate-400 border-slate-700'
  }
}

watch(
  () => props.runId,
  () => {
    logs.value = []
    connectStream()
  },
)

watch(
  () => props.status,
  (nextStatus, prevStatus) => {
    if (
      (nextStatus === 'queued' || nextStatus === 'running') &&
      prevStatus !== nextStatus &&
      !eventSource
    ) {
      connectStream()
    }
  },
)

onMounted(() => {
  connectStream()
})

onBeforeUnmount(() => {
  closeStream()
})
</script>

<template>
  <div class="overflow-hidden rounded-xl border border-slate-800 bg-slate-950 shadow-sm">
    <!-- Terminal Toolbar -->
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 bg-slate-900/90 px-4 py-2.5">
      <div class="flex items-center gap-3">
        <div class="flex items-center gap-2 text-slate-200">
          <Terminal class="h-4 w-4 text-indigo-400" />
          <span class="text-xs font-semibold tracking-wide uppercase">{{ t('sse.title') }}</span>
        </div>
        <span
          class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium"
          :class="stateMeta.badgeClass"
        >
          <span class="h-1.5 w-1.5 rounded-full" :class="stateMeta.dotClass" />
          {{ stateMeta.label }}
        </span>
      </div>

      <div class="flex items-center gap-2">
        <label class="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-slate-700 bg-slate-800/80 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-600">
          <input
            v-model="autoScroll"
            type="checkbox"
            class="h-3.5 w-3.5 rounded border-slate-600 bg-slate-900 text-indigo-500 focus:ring-indigo-500"
          />
          <span>{{ t('sse.autoScroll') }}</span>
        </label>

        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800/80 px-2.5 py-1 text-xs text-slate-300 transition hover:bg-slate-700 hover:text-white"
          :title="t('sse.reconnect')"
          @click="connectStream"
        >
          <RefreshCw class="h-3.5 w-3.5" />
          <span>{{ t('sse.reconnect') }}</span>
        </button>

        <button
          type="button"
          :disabled="logs.length === 0"
          class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800/80 px-2.5 py-1 text-xs text-slate-300 transition hover:bg-slate-700 hover:text-white disabled:opacity-40"
          @click="copyLogs"
        >
          <Check v-if="copied" class="h-3.5 w-3.5 text-emerald-400" />
          <Copy v-else class="h-3.5 w-3.5" />
          <span>{{ copied ? t('sse.copied') : t('sse.copy') }}</span>
        </button>

        <button
          type="button"
          :disabled="logs.length === 0"
          class="inline-flex items-center gap-1 rounded-md border border-slate-700 bg-slate-800/80 px-2.5 py-1 text-xs text-slate-300 transition hover:bg-rose-950/60 hover:text-rose-300 disabled:opacity-40"
          @click="clearLogs"
        >
          <Trash2 class="h-3.5 w-3.5" />
          <span>{{ t('sse.clear') }}</span>
        </button>
      </div>
    </div>

    <!-- Console Output -->
    <div
      ref="scrollContainer"
      class="h-80 overflow-y-auto px-4 py-3 font-mono text-xs leading-relaxed"
    >
      <div
        v-if="logs.length === 0"
        class="flex h-full items-center justify-center text-slate-500"
      >
        {{ t('sse.waiting') }}
      </div>

      <div v-else class="space-y-1">
        <div
          v-for="entry in logs"
          :key="entry.id"
          class="flex items-start gap-2.5 rounded px-1.5 py-0.5 hover:bg-slate-900/80"
        >
          <span class="shrink-0 select-none text-slate-500">{{ entry.timestamp }}</span>
          <span
            class="shrink-0 rounded border px-1.5 py-0 text-[10px] font-medium uppercase"
            :class="eventPillClass(entry.level)"
          >
            {{ entry.event }}
          </span>
          <span class="min-w-0 break-words whitespace-pre-wrap" :class="levelTextClass(entry.level)">
            {{ entry.message }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
