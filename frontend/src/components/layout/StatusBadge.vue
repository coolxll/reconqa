<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "@/i18n";

const props = withDefaults(
  defineProps<{
    status: string;
    size?: "sm" | "md";
  }>(),
  {
    size: "sm",
  }
);

const { t } = useI18n();

const normalized = computed(() => (props.status || "unknown").toLowerCase().trim());

interface BadgeStyle {
  wrapper: string;
  dot: string;
  pulse?: boolean;
}

const badgeStyle = computed<BadgeStyle>(() => {
  const s = normalized.value;
  if (
    ["completed", "success", "passed", "available", "ok", "blocked", "covered"].includes(s)
  ) {
    return {
      wrapper: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
      dot: "bg-emerald-500",
    };
  }
  if (["har-live"].includes(s)) {
    return {
      wrapper: "bg-emerald-50 text-emerald-800 ring-emerald-600/25 font-mono",
      dot: "bg-emerald-500",
    };
  }
  if (["running", "in_progress"].includes(s)) {
    return {
      wrapper: "bg-sky-50 text-sky-700 ring-sky-600/20",
      dot: "bg-sky-500",
      pulse: true,
    };
  }
  if (["queued", "account_a"].includes(s)) {
    return {
      wrapper: "bg-indigo-50 text-indigo-700 ring-indigo-600/20",
      dot: "bg-indigo-500",
    };
  }
  if (["js-static-inferred", "account_b"].includes(s)) {
    return {
      wrapper: "bg-violet-50 text-violet-700 ring-violet-600/20",
      dot: "bg-violet-500",
    };
  }
  if (
    ["failed", "critical", "high", "error", "unauth_bypass", "confirmed_idor"].includes(s)
  ) {
    return {
      wrapper: "bg-rose-50 text-rose-700 ring-rose-600/20",
      dot: "bg-rose-500",
    };
  }
  if (
    ["medium", "suspected_idor", "mutation_candidate", "warning", "broken", "js-static"].includes(s)
  ) {
    return {
      wrapper: "bg-amber-50 text-amber-800 ring-amber-600/20",
      dot: "bg-amber-500",
    };
  }
  if (["low", "pending", "info", "skipped", "guest", "missing"].includes(s)) {
    return {
      wrapper: "bg-slate-100 text-slate-700 ring-slate-500/20",
      dot: "bg-slate-400",
    };
  }
  return {
    wrapper: "bg-slate-100 text-slate-700 ring-slate-500/20",
    dot: "bg-slate-400",
  };
});

const sizeClass = computed(() =>
  props.size === "md" ? "px-2.5 py-1 text-xs gap-1.5" : "px-2 py-0.5 text-[11px] gap-1.5"
);

const displayLabel = computed(() => {
  const key = `status.${normalized.value}`;
  const translated = t(key);
  if (translated !== key) {
    return translated;
  }
  return (props.status || "unknown").replace(/_/g, " ");
});
</script>

<template>
  <span
    class="inline-flex items-center whitespace-nowrap rounded-full font-medium ring-1 ring-inset"
    :class="[badgeStyle.wrapper, sizeClass]"
  >
    <span
      class="h-1.5 w-1.5 shrink-0 rounded-full"
      :class="[badgeStyle.dot, badgeStyle.pulse ? 'animate-pulse' : '']"
    />
    <span>{{ displayLabel }}</span>
  </span>
</template>
