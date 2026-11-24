<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  Activity,
  Compass,
  FolderKanban,
  LayoutDashboard,
  PanelLeftClose,
  PanelLeftOpen,
  PlayCircle,
  Radar,
  TrendingUp,
} from "lucide-vue-next";
import { useI18n } from "@/i18n";

const props = withDefaults(
  defineProps<{
    collapsed?: boolean;
  }>(),
  {
    collapsed: false,
  }
);

const emit = defineEmits<{
  toggle: [];
}>();

const route = useRoute();
const { t } = useI18n();
const apiOnline = ref<boolean | null>(null);

const navItems = computed(() => [
  {
    name: "overview",
    to: "/",
    label: t("nav.overview"),
    hint: t("nav.overviewHint"),
    icon: LayoutDashboard,
  },
  {
    name: "projects",
    to: "/projects",
    label: t("nav.projects"),
    hint: t("nav.projectsHint"),
    icon: FolderKanban,
  },
  {
    name: "explore",
    to: "/explore",
    label: t("nav.explore"),
    hint: t("nav.exploreHint"),
    icon: Compass,
  },
  {
    name: "runs",
    to: "/runs",
    label: t("nav.runs"),
    hint: t("nav.runsHint"),
    icon: PlayCircle,
  },
  {
    name: "trends",
    to: "/trends",
    label: t("nav.trends"),
    hint: t("nav.trendsHint"),
    icon: TrendingUp,
  },
]);

const activeName = computed(() => {
  if (route.name === "run-detail") {
    return "runs";
  }
  return String(route.name || "overview");
});

const apiStatusLabel = computed(() =>
  apiOnline.value === true
    ? t("nav.online")
    : apiOnline.value === false
      ? t("nav.offline")
      : t("nav.checking")
);

const toggleLabel = computed(() =>
  props.collapsed ? t("nav.expandSidebar") : t("nav.collapseSidebar")
);

onMounted(async () => {
  try {
    const res = await fetch("/healthz");
    apiOnline.value = res.ok;
  } catch {
    apiOnline.value = false;
  }
});
</script>

<template>
  <aside
    class="flex h-full shrink-0 flex-col overflow-hidden border-r border-slate-800 bg-slate-900 text-slate-200 transition-[width] duration-200 ease-in-out"
    :class="collapsed ? 'w-[68px]' : 'w-64'"
    data-testid="sidebar-nav"
    :data-collapsed="collapsed ? 'true' : 'false'"
  >
    <!-- Brand -->
    <div
      class="flex h-16 shrink-0 items-center border-b border-slate-800"
      :class="collapsed ? 'justify-center px-3' : 'gap-3 px-5'"
    >
      <div
        class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-indigo-600 text-white shadow-md shadow-indigo-950/40"
        :title="collapsed ? 'ReconQA' : undefined"
      >
        <Radar class="h-5 w-5" />
      </div>
      <div v-if="!collapsed" class="min-w-0 overflow-hidden whitespace-nowrap">
        <div class="flex items-center gap-2">
          <span class="text-base font-bold tracking-tight text-white">ReconQA</span>
          <span
            class="rounded bg-indigo-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-300 ring-1 ring-inset ring-indigo-400/30"
          >
            v0.1
          </span>
        </div>
        <p class="truncate text-[11px] text-slate-400">{{ t("nav.brandTagline") }}</p>
      </div>
    </div>

    <!-- Navigation -->
    <nav class="flex-1 space-y-1 overflow-y-auto overflow-x-hidden py-4" :class="collapsed ? 'px-2' : 'px-3'">
      <p
        v-if="!collapsed"
        class="whitespace-nowrap px-2.5 pb-1.5 text-[10px] font-semibold uppercase tracking-widest text-slate-500"
      >
        {{ t("nav.console") }}
      </p>
      <router-link
        v-for="item in navItems"
        :key="item.name"
        :to="item.to"
        :title="collapsed ? item.label : undefined"
        :aria-label="item.label"
        class="group flex items-center rounded-lg text-sm transition"
        :class="[
          collapsed ? 'justify-center px-0 py-2.5' : 'gap-3 px-3 py-2.5',
          activeName === item.name
            ? 'bg-indigo-600/90 text-white shadow-sm'
            : 'text-slate-300 hover:bg-slate-800 hover:text-white',
        ]"
      >
        <component
          :is="item.icon"
          class="h-4 w-4 shrink-0"
          :class="
            activeName === item.name ? 'text-white' : 'text-slate-400 group-hover:text-slate-200'
          "
        />
        <div v-if="!collapsed" class="min-w-0 flex-1 overflow-hidden whitespace-nowrap">
          <div class="truncate font-medium leading-tight">{{ item.label }}</div>
          <div
            class="mt-0.5 truncate text-[11px] leading-tight"
            :class="activeName === item.name ? 'text-indigo-100/80' : 'text-slate-500'"
          >
            {{ item.hint }}
          </div>
        </div>
      </router-link>
    </nav>

    <!-- Footer status & collapse toggle -->
    <div
      class="shrink-0 border-t border-slate-800 text-xs"
      :class="collapsed ? 'flex flex-col items-center gap-2.5 px-2 py-3.5' : 'px-4 py-3.5'"
    >
      <div
        v-if="!collapsed"
        class="flex items-center justify-between overflow-hidden whitespace-nowrap rounded-lg bg-slate-800/70 px-3 py-2"
      >
        <div class="flex items-center gap-2">
          <Activity class="h-3.5 w-3.5 shrink-0 text-slate-400" />
          <span class="text-slate-300">{{ t("nav.backendCore") }}</span>
        </div>
        <span class="inline-flex items-center gap-1.5 font-medium">
          <span
            class="h-2 w-2 rounded-full"
            :class="
              apiOnline === true
                ? 'bg-emerald-400'
                : apiOnline === false
                  ? 'bg-rose-400'
                  : 'bg-slate-500 animate-pulse'
            "
          />
          <span
            :class="
              apiOnline === true
                ? 'text-emerald-300'
                : apiOnline === false
                  ? 'text-rose-300'
                  : 'text-slate-400'
            "
          >
            {{ apiStatusLabel }}
          </span>
        </span>
      </div>

      <div
        v-else
        class="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-800/70"
        :title="`${t('nav.backendCore')}: ${apiStatusLabel}`"
      >
        <span
          class="h-2.5 w-2.5 rounded-full"
          :class="
            apiOnline === true
              ? 'bg-emerald-400'
              : apiOnline === false
                ? 'bg-rose-400'
                : 'bg-slate-500 animate-pulse'
          "
        />
      </div>

      <button
        type="button"
        class="flex items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-800 hover:text-slate-100"
        :class="collapsed ? 'h-8 w-8' : 'mt-2 w-full gap-2 px-3 py-1.5 text-[11px]'"
        :title="toggleLabel"
        :aria-label="toggleLabel"
        data-testid="sidebar-footer-toggle"
        @click="emit('toggle')"
      >
        <PanelLeftOpen v-if="collapsed" class="h-4 w-4 shrink-0" />
        <PanelLeftClose v-else class="h-4 w-4 shrink-0" />
        <span v-if="!collapsed" class="truncate whitespace-nowrap">{{ toggleLabel }}</span>
      </button>

      <p
        v-if="!collapsed"
        class="mt-2 truncate whitespace-nowrap text-center text-[11px] text-slate-500"
      >
        {{ t("nav.footerSpec") }}
      </p>
    </div>
  </aside>
</template>
