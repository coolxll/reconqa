<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    label: string;
    value: string | number;
    subtitle?: string;
    tone?: "neutral" | "indigo" | "emerald" | "amber" | "rose";
  }>(),
  {
    subtitle: undefined,
    tone: "neutral",
  }
);

const toneClasses = computed(() => {
  switch (props.tone) {
    case "indigo":
      return {
        border: "border-indigo-200/80 bg-gradient-to-br from-white to-indigo-50/40",
        accent: "bg-indigo-500",
        value: "text-indigo-700",
      };
    case "emerald":
      return {
        border: "border-emerald-200/80 bg-gradient-to-br from-white to-emerald-50/40",
        accent: "bg-emerald-500",
        value: "text-emerald-700",
      };
    case "amber":
      return {
        border: "border-amber-200/80 bg-gradient-to-br from-white to-amber-50/40",
        accent: "bg-amber-500",
        value: "text-amber-700",
      };
    case "rose":
      return {
        border: "border-rose-200/80 bg-gradient-to-br from-white to-rose-50/40",
        accent: "bg-rose-500",
        value: "text-rose-700",
      };
    default:
      return {
        border: "border-slate-200/90 bg-white",
        accent: "bg-slate-400",
        value: "text-slate-900",
      };
  }
});
</script>

<template>
  <div
    class="relative overflow-hidden rounded-xl border p-4 shadow-sm transition hover:shadow"
    :class="toneClasses.border"
  >
    <div class="absolute inset-y-0 left-0 w-1" :class="toneClasses.accent" />
    <div class="flex items-start justify-between gap-2 pl-2">
      <div class="min-w-0">
        <p class="truncate text-xs font-medium uppercase tracking-wider text-slate-500">
          {{ label }}
        </p>
        <p
          class="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums"
          :class="toneClasses.value"
        >
          {{ value }}
        </p>
        <p v-if="subtitle" class="mt-1 truncate text-xs text-slate-500">
          {{ subtitle }}
        </p>
      </div>
      <div v-if="$slots.icon" class="shrink-0 text-slate-400">
        <slot name="icon" />
      </div>
    </div>
    <div v-if="$slots.footer" class="mt-3 border-t border-slate-100 pl-2 pt-2.5 text-xs">
      <slot name="footer" />
    </div>
  </div>
</template>
