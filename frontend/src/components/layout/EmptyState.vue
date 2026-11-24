<script setup lang="ts">
import { Inbox } from "lucide-vue-next";

defineProps<{
  title: string;
  description?: string;
}>();
</script>

<template>
  <div
    class="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-300 bg-slate-50/60 px-6 py-12 text-center"
  >
    <div
      class="flex h-11 w-11 items-center justify-center rounded-full bg-white text-slate-400 shadow-sm ring-1 ring-slate-200"
    >
      <slot name="icon">
        <Inbox class="h-5 w-5" />
      </slot>
    </div>
    <h3 class="mt-3.5 text-sm font-semibold text-slate-800">{{ title }}</h3>
    <p v-if="description" class="mt-1 max-w-md text-xs leading-relaxed text-slate-500">
      {{ description }}
    </p>
    <div v-if="$slots.action || $slots.default" class="mt-4 flex items-center gap-2">
      <slot name="action" />
      <slot />
    </div>
  </div>
</template>
