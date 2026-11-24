<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  Archive,
  Check,
  Copy,
  Download,
  FileCode2,
  FileJson,
  FolderArchive,
  Gauge,
  Package,
  Shield,
} from 'lucide-vue-next'
import type { Artifact } from '@/api/types'
import { artifactDownloadUrl } from '@/api/client'
import { listRunArtifacts } from '@/api/runs'
import EmptyState from '@/components/layout/EmptyState.vue'
import '@/i18n/locales/runs'
import { useI18n } from '@/i18n'

const { t } = useI18n()

interface ArtifactGroup {
  id: string
  title: string
  subtitle: string
  items: Artifact[]
}

const props = defineProps<{
  runId: string
}>()

const loading = ref(true)
const errorMessage = ref('')
const artifacts = ref<Artifact[]>([])
const copiedHash = ref<string | null>(null)

async function loadArtifacts() {
  loading.value = true
  errorMessage.value = ''
  try {
    artifacts.value = await listRunArtifacts(props.runId)
  } catch (err) {
    errorMessage.value =
      err instanceof Error ? err.message : t('artifacts.emptyDesc')
    artifacts.value = []
  } finally {
    loading.value = false
  }
}

const availableKinds = computed(() => new Set(artifacts.value.map((a) => a.kind)))

const quickBundles = computed(() => [
  {
    kind: 'java-tests',
    label: t('artifacts.bundle.java'),
    badge: 'ZIP',
    icon: Package,
    available: availableKinds.value.has('java_tests') || availableKinds.value.has('java-tests'),
  },
  {
    kind: 'burp_exports',
    label: t('artifacts.bundle.burp'),
    badge: 'ZIP',
    icon: Shield,
    available: availableKinds.value.has('burp_exports') || availableKinds.value.has('burp-exports'),
  },
  {
    kind: 'k6',
    label: t('artifacts.bundle.k6'),
    badge: 'ZIP',
    icon: Gauge,
    available: availableKinds.value.has('k6_dir') || availableKinds.value.has('k6_script'),
  },
  {
    kind: 'openapi_yaml',
    label: t('artifacts.bundle.openapi'),
    badge: 'YAML',
    icon: FileCode2,
    available: availableKinds.value.has('openapi_yaml') || availableKinds.value.has('openapi_json'),
  },
  {
    kind: 'correlation_json',
    label: t('artifacts.bundle.correlation'),
    badge: 'JSON',
    icon: FileJson,
    available: availableKinds.value.has('correlation_json'),
  },
  {
    kind: 'har',
    label: t('artifacts.bundle.har'),
    badge: 'HAR',
    icon: Archive,
    available: availableKinds.value.has('har'),
  },
])

function categoryForKind(kind: string): string {
  if (
    [
      'openapi_yaml',
      'openapi_json',
      'correlation_json',
      'correlation_mmd',
      'coverage_diff_json',
      'coverage_diff_md',
      'js_endpoints_json',
      'spa_routes_json',
    ].includes(kind)
  ) {
    return 'hub'
  }
  if (['java_tests', 'java-tests', 'schemathesis_report'].includes(kind)) {
    return 'java'
  }
  if (
    [
      'burp_exports',
      'burp-exports',
      'static_audit_json',
      'static_audit_md',
      'auth_matrix_json',
      'auth_matrix_md',
      'nuclei_jsonl',
    ].includes(kind)
  ) {
    return 'security'
  }
  if (['k6_script', 'k6_dir', 'k6', 'k6_analysis_json', 'har_perf_json', 'har_perf_md'].includes(kind)) {
    return 'perf'
  }
  return 'recordings'
}

const groupedArtifacts = computed<ArtifactGroup[]>(() => {
  const defs: Array<Omit<ArtifactGroup, 'items'>> = [
    {
      id: 'hub',
      title: t('artifacts.group.hub.title'),
      subtitle: t('artifacts.group.hub.subtitle'),
    },
    {
      id: 'java',
      title: t('artifacts.group.java.title'),
      subtitle: t('artifacts.group.java.subtitle'),
    },
    {
      id: 'security',
      title: t('artifacts.group.security.title'),
      subtitle: t('artifacts.group.security.subtitle'),
    },
    {
      id: 'perf',
      title: t('artifacts.group.perf.title'),
      subtitle: t('artifacts.group.perf.subtitle'),
    },
    {
      id: 'recordings',
      title: t('artifacts.group.recordings.title'),
      subtitle: t('artifacts.group.recordings.subtitle'),
    },
  ]

  const map = new Map<string, Artifact[]>()
  for (const item of artifacts.value) {
    const cat = categoryForKind(item.kind)
    const list = map.get(cat) ?? []
    list.push(item)
    map.set(cat, list)
  }

  return defs
    .map((d) => ({ ...d, items: map.get(d.id) ?? [] }))
    .filter((g) => g.items.length > 0)
})

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

function isArchiveDir(kind: string): boolean {
  return ['java_tests', 'java-tests', 'burp_exports', 'burp-exports', 'k6_dir', 'k6'].includes(kind)
}

async function copySha(hash: string) {
  if (!hash) return
  try {
    await navigator.clipboard.writeText(hash)
    copiedHash.value = hash
    window.setTimeout(() => {
      copiedHash.value = null
    }, 1500)
  } catch {
    // ignore clipboard errors
  }
}

watch(() => props.runId, loadArtifacts)
onMounted(loadArtifacts)
</script>

<template>
  <div class="space-y-6">
    <div
      v-if="loading"
      class="flex items-center justify-center rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500"
    >
      {{ t('artifacts.loading') }}
    </div>

    <EmptyState
      v-else-if="artifacts.length === 0"
      :title="t('artifacts.emptyTitle')"
      :description="errorMessage || t('artifacts.emptyDesc')"
    />

    <template v-else>
      <!-- One-Click Featured Bundles -->
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <a
          v-for="bundle in quickBundles"
          :key="bundle.kind"
          :href="bundle.available ? artifactDownloadUrl(runId, bundle.kind) : undefined"
          :download="bundle.available ? '' : undefined"
          class="flex items-center justify-between rounded-xl border px-4 py-3.5 transition"
          :class="
            bundle.available
              ? 'border-slate-200 bg-white shadow-sm hover:border-indigo-300 hover:bg-indigo-50/30'
              : 'pointer-events-none border-slate-200/70 bg-slate-50 opacity-50'
          "
        >
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
              <component :is="bundle.icon" class="h-4 w-4" />
            </div>
            <div>
              <div class="text-sm font-semibold text-slate-900">{{ bundle.label }}</div>
              <div class="text-xs text-slate-500">
                {{ bundle.available ? t('artifacts.ready') : t('artifacts.notGenerated') }}
              </div>
            </div>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-slate-600">
              {{ bundle.badge }}
            </span>
            <Download v-if="bundle.available" class="h-4 w-4 text-indigo-600" />
          </div>
        </a>
      </div>

      <!-- Grouped Artifacts Inventory -->
      <div
        v-for="group in groupedArtifacts"
        :key="group.id"
        class="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm"
      >
        <div class="border-b border-slate-200 bg-slate-50/70 px-5 py-3.5">
          <h3 class="text-sm font-semibold text-slate-900">{{ group.title }}</h3>
          <p class="mt-0.5 text-xs text-slate-500">{{ group.subtitle }}</p>
        </div>

        <div class="overflow-x-auto">
          <table class="min-w-full divide-y divide-slate-200 text-left text-xs">
            <thead class="bg-white font-semibold tracking-wider text-slate-400 uppercase">
              <tr>
                <th class="px-5 py-2.5">{{ t('artifacts.col.kind') }}</th>
                <th class="px-4 py-2.5">{{ t('artifacts.col.path') }}</th>
                <th class="px-4 py-2.5 text-right">{{ t('artifacts.col.size') }}</th>
                <th class="px-4 py-2.5">{{ t('artifacts.col.sha') }}</th>
                <th class="px-5 py-2.5 text-right">{{ t('artifacts.col.download') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr
                v-for="art in group.items"
                :key="art.id"
                class="hover:bg-slate-50/80"
              >
                <td class="px-5 py-3">
                  <span class="inline-flex items-center gap-1.5 rounded-md bg-slate-100 px-2 py-0.5 font-mono font-medium text-slate-700">
                    <FolderArchive v-if="isArchiveDir(art.kind)" class="h-3 w-3 text-indigo-500" />
                    {{ art.kind }}
                  </span>
                </td>
                <td class="px-4 py-3 font-mono text-slate-800">{{ art.rel_path }}</td>
                <td class="px-4 py-3 text-right font-mono text-slate-600">
                  {{ formatSize(art.size_bytes) }}
                </td>
                <td class="px-4 py-3">
                  <button
                    v-if="art.sha256"
                    type="button"
                    class="inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[11px] text-slate-500 transition hover:bg-slate-100 hover:text-slate-800"
                    :title="art.sha256"
                    @click="copySha(art.sha256)"
                  >
                    <span>{{ art.sha256.slice(0, 12) }}…</span>
                    <Check v-if="copiedHash === art.sha256" class="h-3 w-3 text-emerald-600" />
                    <Copy v-else class="h-3 w-3" />
                  </button>
                  <span v-else class="text-slate-400">{{ t('artifacts.dirZip') }}</span>
                </td>
                <td class="px-5 py-3 text-right">
                  <a
                    :href="artifactDownloadUrl(runId, art.kind)"
                    download
                    class="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-indigo-700 transition hover:border-indigo-300 hover:bg-indigo-50"
                  >
                    <Download class="h-3.5 w-3.5" />
                    <span>{{ isArchiveDir(art.kind) ? 'ZIP' : t('artifacts.downloadBtn') }}</span>
                  </a>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>
