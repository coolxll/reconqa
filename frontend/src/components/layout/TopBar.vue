<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { ChevronRight, Globe, PanelLeftClose, PanelLeftOpen, Plus } from "lucide-vue-next";
import { useI18n } from "@/i18n";

const props = withDefaults(
  defineProps<{
    sidebarCollapsed?: boolean;
  }>(),
  {
    sidebarCollapsed: false,
  }
);

const emit = defineEmits<{
  toggleSidebar: [];
}>();

const route = useRoute();
const { locale, setLocale, t } = useI18n();

const routeKeyMap: Record<string, string> = {
  overview: "overview",
  projects: "projects",
  explore: "explore",
  runs: "runs",
  "run-detail": "runDetail",
  trends: "trends",
};

const routeKey = computed(() => routeKeyMap[String(route.name || "overview")] ?? "overview");
const title = computed(() => t(`route.${routeKey.value}.title`));
const subtitle = computed(() => t(`route.${routeKey.value}.subtitle`));
const runIdParam = computed(() =>
  route.name === "run-detail" ? String(route.params.runId || "") : ""
);
const sidebarToggleLabel = computed(() =>
  props.sidebarCollapsed ? t("nav.expandSidebar") : t("nav.collapseSidebar")
);
</script>

<template>
  <header
    class="sticky top-0 z-20 flex h-16 shrink-0 items-center justify-between gap-4 border-b border-slate-200 bg-white/95 px-5 backdrop-blur"
  >
    <div class="flex min-w-0 items-center gap-3">
      <button
        type="button"
        class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-600 shadow-sm transition hover:bg-slate-50 hover:text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
        :title="sidebarToggleLabel"
        :aria-label="sidebarToggleLabel"
        :aria-expanded="!sidebarCollapsed"
        data-testid="sidebar-toggle"
        @click="emit('toggleSidebar')"
      >
        <PanelLeftOpen v-if="sidebarCollapsed" class="h-4 w-4" />
        <PanelLeftClose v-else class="h-4 w-4" />
      </button>

      <div class="min-w-0">
        <div class="flex items-center gap-2">
          <template v-if="runIdParam">
            <router-link
              to="/runs"
              class="text-sm font-medium text-slate-500 transition hover:text-indigo-600"
            >
              {{ t("route.runs.title") }}
            </router-link>
            <ChevronRight class="h-4 w-4 text-slate-400" />
            <h1 class="truncate font-mono text-sm font-semibold text-slate-900">
              {{ runIdParam }}
            </h1>
          </template>
          <template v-else>
            <h1 class="truncate text-lg font-semibold tracking-tight text-slate-900">
              {{ title }}
            </h1>
          </template>
        </div>
        <p v-if="subtitle" class="truncate text-xs text-slate-500">
          {{ subtitle }}
        </p>
      </div>
    </div>

    <div class="flex shrink-0 items-center gap-2.5">
      <!-- Bilingual ZH / EN Segmented Switcher -->
      <div
        class="inline-flex items-center rounded-lg border border-slate-200 bg-slate-100/90 p-0.5 text-xs font-medium"
        role="group"
        :aria-label="t('topbar.switchLang')"
        data-testid="locale-switcher"
      >
        <Globe class="ml-1.5 mr-1 h-3.5 w-3.5 text-slate-400" />
        <button
          type="button"
          class="rounded-md px-2 py-1 transition"
          :class="
            locale === 'zh'
              ? 'bg-white text-indigo-700 shadow-sm ring-1 ring-slate-200/80'
              : 'text-slate-600 hover:text-slate-900'
          "
          :aria-pressed="locale === 'zh'"
          data-testid="locale-btn-zh"
          @click="setLocale('zh')"
        >
          中文
        </button>
        <button
          type="button"
          class="rounded-md px-2 py-1 transition"
          :class="
            locale === 'en'
              ? 'bg-white text-indigo-700 shadow-sm ring-1 ring-slate-200/80'
              : 'text-slate-600 hover:text-slate-900'
          "
          :aria-pressed="locale === 'en'"
          data-testid="locale-btn-en"
          @click="setLocale('en')"
        >
          EN
        </button>
      </div>

      <router-link to="/projects" class="btn-secondary hidden sm:inline-flex">
        {{ t("topbar.projects") }}
      </router-link>
      <router-link :to="{ name: 'runs', query: { new: '1' } }" class="btn-primary">
        <Plus class="h-4 w-4" />
        <span class="hidden sm:inline">{{ t("topbar.importHar") }}</span>
        <span class="sm:hidden">{{ t("topbar.importHarShort") }}</span>
      </router-link>
    </div>
  </header>
</template>
