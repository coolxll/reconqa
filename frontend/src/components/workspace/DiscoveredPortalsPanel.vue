<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { CheckCircle2, Globe, Loader2, Plus, Sparkles } from "lucide-vue-next";
import { batchCreateTargets } from "@/api/projects";
import type { BatchTargetItem, DiscoveredPortal, Target } from "@/api/types";
import { useI18n } from "@/i18n";

const props = defineProps<{
  projectId: string;
  portals: DiscoveredPortal[];
  existingTargets: Target[];
  rootDomain?: string;
}>();

const emit = defineEmits<{
  added: [added: Target[], skipped: number];
  error: [message: string];
}>();

const { t } = useI18n();

const selectedUrls = ref<Set<string>>(new Set());
const labelEdits = ref<Record<string, string>>({});
const adding = ref(false);

function normalizeBaseUrl(raw: string): string {
  return (raw || "").trim().replace(/\/+$/, "").toLowerCase();
}

function extractHost(raw: string): string {
  try {
    return new URL(raw).host.toLowerCase();
  } catch {
    return (raw || "").trim().toLowerCase();
  }
}

function inferRootDomain(host: string): string {
  if (props.rootDomain) return props.rootDomain;
  const clean = host.split(":")[0].toLowerCase();
  const parts = clean.split(".").filter(Boolean);
  if (parts.length <= 2 || /^\d+\.\d+\.\d+\.\d+$/.test(clean)) return clean;
  const twoPartTlds = new Set(["com.cn", "net.cn", "org.cn", "gov.cn", "co.uk", "com.hk"]);
  const lastTwo = parts.slice(-2).join(".");
  if (twoPartTlds.has(lastTwo) && parts.length >= 3) {
    return parts.slice(-3).join(".");
  }
  return lastTwo;
}

const existingUrlSet = computed(() => {
  const set = new Set<string>();
  for (const tgt of props.existingTargets) {
    set.add(normalizeBaseUrl(tgt.base_url));
    set.add(extractHost(tgt.base_url));
  }
  return set;
});

function isAlreadyAdded(portal: DiscoveredPortal): boolean {
  return (
    existingUrlSet.value.has(normalizeBaseUrl(portal.url)) ||
    existingUrlSet.value.has(portal.host.toLowerCase())
  );
}

watch(
  () => [props.portals, props.existingTargets] as const,
  ([portals]) => {
    const nextSelected = new Set<string>();
    const nextLabels: Record<string, string> = { ...labelEdits.value };
    for (const p of portals) {
      if (!(p.url in nextLabels)) {
        nextLabels[p.url] = p.suggested_label || p.title || "";
      }
      if (!isAlreadyAdded(p) && !p.is_api_gateway) {
        nextSelected.add(p.url);
      }
    }
    labelEdits.value = nextLabels;
    selectedUrls.value = nextSelected;
  },
  { immediate: true }
);

function toggleUrl(url: string): void {
  const next = new Set(selectedUrls.value);
  if (next.has(url)) next.delete(url);
  else next.add(url);
  selectedUrls.value = next;
}

async function handleBatchAdd(): Promise<void> {
  if (!props.projectId || selectedUrls.value.size === 0 || adding.value) return;
  adding.value = true;
  try {
    const payload: BatchTargetItem[] = props.portals
      .filter((p) => selectedUrls.value.has(p.url) && !isAlreadyAdded(p))
      .map((p) => ({
        root_domain: inferRootDomain(p.host),
        base_url: p.url,
        portal_label: (labelEdits.value[p.url] || p.suggested_label || "").trim() || undefined,
      }));
    if (payload.length === 0) return;
    const res = await batchCreateTargets(props.projectId, payload);
    emit("added", res.added, res.skipped);
  } catch (err) {
    emit("error", err instanceof Error ? err.message : t("settings.target.errBatchAdd"));
  } finally {
    adding.value = false;
  }
}
</script>

<template>
  <div
    data-testid="discovered-portals-panel"
    class="overflow-hidden rounded-xl border border-emerald-200/90 bg-emerald-50/40"
  >
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-emerald-200/70 bg-emerald-50/80 px-4 py-2.5">
      <div class="flex items-center gap-2 text-xs font-semibold text-emerald-950">
        <Sparkles class="h-3.5 w-3.5 text-emerald-600" />
        <span>{{ t("settings.target.discoveredTitle", { count: portals.length }) }}</span>
      </div>

      <button
        v-if="portals.length > 0"
        type="button"
        data-testid="batch-add-portals-btn"
        class="btn-primary !px-3 !py-1 !text-xs"
        :disabled="selectedUrls.size === 0 || adding"
        @click="handleBatchAdd"
      >
        <Loader2 v-if="adding" class="h-3.5 w-3.5 animate-spin" />
        <Plus v-else class="h-3.5 w-3.5" />
        <span>
          {{
            adding
              ? t("settings.target.batchAddingBtn")
              : t("settings.target.batchAddBtn", { count: selectedUrls.size })
          }}
        </span>
      </button>
    </div>

    <div v-if="portals.length === 0" class="px-4 py-4 text-xs text-slate-500">
      {{ t("settings.target.discoveredEmpty") }}
    </div>

    <div v-else class="divide-y divide-emerald-100 bg-white">
      <div
        v-for="portal in portals"
        :key="portal.url"
        data-testid="discovered-portal-row"
        class="flex flex-col justify-between gap-2.5 px-4 py-2.5 sm:flex-row sm:items-center"
      >
        <div class="flex min-w-0 flex-1 items-start gap-2.5">
          <input
            type="checkbox"
            :checked="selectedUrls.has(portal.url)"
            :disabled="isAlreadyAdded(portal)"
            class="mt-1 h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500 disabled:opacity-40"
            @change="toggleUrl(portal.url)"
          />
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-1.5">
              <Globe class="h-3.5 w-3.5 shrink-0 text-indigo-500" />
              <span class="truncate font-mono text-xs font-semibold text-slate-900">
                {{ portal.host }}
              </span>
              <span
                class="rounded px-1.5 py-0.5 font-mono text-[10px] font-bold"
                :class="
                  portal.status_code >= 200 && portal.status_code < 400
                    ? 'bg-emerald-50 text-emerald-700'
                    : 'bg-slate-100 text-slate-600'
                "
              >
                {{ portal.status_code }}
              </span>
              <span
                v-if="portal.is_api_gateway"
                class="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800"
              >
                {{ t("settings.target.apiGatewayTag") }}
              </span>
              <span
                v-if="isAlreadyAdded(portal)"
                class="inline-flex items-center gap-0.5 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600"
              >
                <CheckCircle2 class="h-3 w-3 text-emerald-600" />
                <span>{{ t("settings.target.alreadyAdded") }}</span>
              </span>
            </div>
            <p v-if="portal.title" class="mt-0.5 truncate text-[11px] text-slate-500">
              {{ portal.title }}
            </p>
          </div>
        </div>

        <div class="shrink-0 sm:w-44">
          <input
            v-if="!isAlreadyAdded(portal)"
            v-model="labelEdits[portal.url]"
            type="text"
            :placeholder="t('settings.target.portalPlaceholder')"
            class="input-field !py-1 text-xs"
          />
        </div>
      </div>
    </div>
  </div>
</template>
