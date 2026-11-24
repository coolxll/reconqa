<script setup lang="ts">
import { onBeforeUnmount, ref } from "vue";
import SidebarNav from "./SidebarNav.vue";
import TopBar from "./TopBar.vue";

const STORAGE_KEY = "reconqa_sidebar_collapsed";

function readInitialCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

const sidebarCollapsed = ref<boolean>(readInitialCollapsed());
let resizeTimer: number | null = null;

function toggleSidebar(): void {
  const next = !sidebarCollapsed.value;
  sidebarCollapsed.value = next;
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(STORAGE_KEY, String(next));
    } catch {
      // Ignore storage access errors
    }
    if (resizeTimer !== null) {
      window.clearTimeout(resizeTimer);
    }
    resizeTimer = window.setTimeout(() => {
      window.dispatchEvent(new Event("resize"));
      resizeTimer = null;
    }, 220);
  }
}

onBeforeUnmount(() => {
  if (resizeTimer !== null && typeof window !== "undefined") {
    window.clearTimeout(resizeTimer);
    resizeTimer = null;
  }
});
</script>

<template>
  <div class="flex h-screen w-screen overflow-hidden bg-slate-50 text-slate-900">
    <SidebarNav :collapsed="sidebarCollapsed" @toggle="toggleSidebar" />
    <div class="flex min-w-0 flex-1 flex-col overflow-hidden">
      <TopBar :sidebar-collapsed="sidebarCollapsed" @toggle-sidebar="toggleSidebar" />
      <main class="flex-1 overflow-y-auto">
        <div class="mx-auto w-full max-w-[1440px] px-6 py-6">
          <slot />
        </div>
      </main>
    </div>
  </div>
</template>
