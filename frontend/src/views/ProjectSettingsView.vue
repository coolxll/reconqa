<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  AlertTriangle,
  CheckCircle2,
  Globe,
  KeyRound,
  Loader2,
  PlayCircle,
  Plus,
  Save,
  Settings,
  ShieldCheck,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-vue-next";
import {
  createAuthProfile,
  createTarget,
  deleteAuthProfile,
  deleteTarget,
  extractCredentialFromHar,
  listAuthProfiles,
  listTargets,
  uploadStorageState,
} from "@/api/projects";
import type { AuthProfile, RoleTag, Target } from "@/api/types";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import EmptyState from "@/components/layout/EmptyState.vue";
import { useWorkspace } from "@/stores/workspace";
import { useI18n } from "@/i18n";

interface HeaderRefRow {
  header: string;
  envRef: string;
}

const route = useRoute();
const router = useRouter();
const { locale, t } = useI18n();
const { activeProject, activeProjectId, updateProject, deleteProject, workspacePath } = useWorkspace();

const projectId = computed(() =>
  typeof route.params.projectId === "string" && route.params.projectId
    ? route.params.projectId
    : activeProjectId.value
);

function resolveTabFromQuery(raw: unknown): "targets" | "auth" | "general" {
  if (raw === "auth" || raw === "credentials") return "auth";
  if (raw === "general") return "general";
  return "targets";
}

const activeTab = ref<"targets" | "auth" | "general">(resolveTabFromQuery(route.query.tab));

watch(
  () => route.query.tab,
  (next) => {
    if (next) {
      activeTab.value = resolveTabFromQuery(next);
    }
  }
);

const targets = ref<Target[]>([]);
const authProfiles = ref<AuthProfile[]>([]);
const loadingDetails = ref(true);
const bannerError = ref<string | null>(null);
const bannerSuccess = ref<string | null>(null);

// Targets form
const addingTarget = ref(false);
const targetPortalLabel = ref("");
const targetRootDomain = ref("");
const targetBaseUrl = ref("");

// Credentials form
const ROLE_PRESETS = ["admin", "pharmacy", "doctor", "assistant", "account_a", "account_b", "guest"] as const;
const addingProfile = ref(false);
const profileName = ref("");
const profileRole = ref<RoleTag>("account_a");
const headerRefRows = ref<HeaderRefRow[]>([
  { header: "Authorization", envRef: "ENV:RECONQA_ACCOUNT_A_TOKEN" },
]);
const uploadingProfileId = ref<string | null>(null);
const extractingFromHar = ref(false);

// General metadata edit form
const editProjectName = ref("");
const editProjectDesc = ref("");
const savingProject = ref(false);

// Delete project confirm
const confirmingDeleteProject = ref(false);
const deletingProject = ref(false);

watch(
  activeProject,
  (proj) => {
    editProjectName.value = proj?.name ?? "";
    editProjectDesc.value = proj?.description ?? "";
  },
  { immediate: true }
);

async function handleSaveProjectMeta(): Promise<void> {
  if (!projectId.value || savingProject.value) return;
  const trimmedName = editProjectName.value.trim();
  if (!trimmedName) {
    notifyError(new Error(t("settings.errEmptyName")), t("settings.errEmptyName"));
    return;
  }
  savingProject.value = true;
  try {
    const updated = await updateProject(projectId.value, {
      name: trimmedName,
      description: editProjectDesc.value.trim(),
    });
    editProjectName.value = updated.name;
    editProjectDesc.value = updated.description ?? "";
    notifySuccess(t("settings.toastUpdated", { name: updated.name }));
  } catch (err) {
    notifyError(err, t("settings.errUpdate"));
  } finally {
    savingProject.value = false;
  }
}

const hasAccountA = computed(() => authProfiles.value.some((p) => p.role_tag === "account_a"));
const hasAccountB = computed(() => authProfiles.value.some((p) => p.role_tag === "account_b"));

function notifySuccess(msg: string): void {
  bannerError.value = null;
  bannerSuccess.value = msg;
  window.setTimeout(() => {
    if (bannerSuccess.value === msg) bannerSuccess.value = null;
  }, 4000);
}

function notifyError(err: unknown, fallback: string): void {
  bannerSuccess.value = null;
  bannerError.value = err instanceof Error ? err.message : fallback;
}

function formatDate(iso: string | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(locale.value === "zh" ? "zh-CN" : "en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function storageStateBasename(fullPath: string | null): string {
  if (!fullPath) return "";
  const parts = fullPath.split(/[\\/]/);
  return parts[parts.length - 1] || fullPath;
}

function maskHeaderValue(val: string): string {
  const trimmed = (val ?? "").trim();
  if (!trimmed) return "—";
  if (trimmed.startsWith("ENV:")) return trimmed;
  if (trimmed.length > 10) {
    return `${trimmed.slice(0, 4)}•••${trimmed.slice(-4)}`;
  }
  if (trimmed.length > 4) {
    return `${trimmed.slice(0, 2)}•••`;
  }
  return "••••";
}

async function loadDetails(): Promise<void> {
  if (!projectId.value) {
    targets.value = [];
    authProfiles.value = [];
    loadingDetails.value = false;
    return;
  }
  loadingDetails.value = true;
  try {
    const [tList, aList] = await Promise.all([
      listTargets(projectId.value),
      listAuthProfiles(projectId.value),
    ]);
    targets.value = tList;
    authProfiles.value = aList;
  } catch (err) {
    notifyError(err, t("projects.err.loadDetails"));
  } finally {
    loadingDetails.value = false;
  }
}

watch(projectId, () => {
  confirmingDeleteProject.value = false;
  void loadDetails();
});

watch(profileRole, (role) => {
  if (
    headerRefRows.value.length === 1 &&
    headerRefRows.value[0].header === "Authorization" &&
    headerRefRows.value[0].envRef.startsWith("ENV:RECONQA_")
  ) {
    headerRefRows.value[0].envRef =
      role === "account_b"
        ? "ENV:RECONQA_ACCOUNT_B_TOKEN"
        : role === "guest"
          ? ""
          : "ENV:RECONQA_ACCOUNT_A_TOKEN";
  }
});

async function handleAddTarget(): Promise<void> {
  if (!projectId.value || !targetRootDomain.value.trim() || !targetBaseUrl.value.trim()) return;
  addingTarget.value = true;
  try {
    const portalLabel = targetPortalLabel.value.trim();
    const created = await createTarget(projectId.value, {
      root_domain: targetRootDomain.value.trim(),
      base_url: targetBaseUrl.value.trim(),
      scope_rules: portalLabel ? { portal_label: portalLabel } : {},
    });
    targets.value = [...targets.value, created];
    targetPortalLabel.value = "";
    targetRootDomain.value = "";
    targetBaseUrl.value = "";
    notifySuccess(t("projects.toast.targetAdded", { domain: created.root_domain }));
  } catch (err) {
    notifyError(err, t("projects.err.createTarget"));
  } finally {
    addingTarget.value = false;
  }
}

function getTargetPortalLabel(target: Target): string {
  const raw = target.scope_rules?.portal_label;
  return typeof raw === "string" ? raw.trim() : "";
}

async function handleDeleteTarget(targetId: string): Promise<void> {
  if (!projectId.value) return;
  try {
    await deleteTarget(projectId.value, targetId);
    targets.value = targets.value.filter((item) => item.id !== targetId);
    notifySuccess(t("projects.toast.targetDeleted"));
  } catch (err) {
    notifyError(err, t("projects.err.deleteTarget"));
  }
}

async function handleAddAuthProfile(): Promise<void> {
  if (!projectId.value || !profileName.value.trim()) return;
  addingProfile.value = true;
  try {
    const extraHeadersRef: Record<string, string> = {};
    for (const row of headerRefRows.value) {
      const k = row.header.trim();
      const v = row.envRef.trim();
      if (k && v) extraHeadersRef[k] = v;
    }
    const created = await createAuthProfile(projectId.value, {
      name: profileName.value.trim(),
      role_tag: profileRole.value,
      extra_headers_ref: extraHeadersRef,
    });
    authProfiles.value = [...authProfiles.value, created];
    profileName.value = "";
    notifySuccess(
      t("projects.toast.profileCreated", { name: created.name, role: created.role_tag })
    );
  } catch (err) {
    notifyError(err, t("projects.err.createProfile"));
  } finally {
    addingProfile.value = false;
  }
}

async function handleDeleteAuthProfile(profileId: string): Promise<void> {
  if (!projectId.value) return;
  try {
    await deleteAuthProfile(projectId.value, profileId);
    authProfiles.value = authProfiles.value.filter((p) => p.id !== profileId);
    notifySuccess(t("projects.toast.profileDeleted"));
  } catch (err) {
    notifyError(err, t("projects.err.deleteProfile"));
  }
}

async function handleExtractFromHar(): Promise<void> {
  if (!projectId.value || extractingFromHar.value) return;
  extractingFromHar.value = true;
  try {
    const res = await extractCredentialFromHar(projectId.value);
    const exists = authProfiles.value.some((p) => p.id === res.profile.id);
    authProfiles.value = exists
      ? authProfiles.value.map((p) => (p.id === res.profile.id ? res.profile : p))
      : [...authProfiles.value, res.profile];
    const headersText = res.extracted_header_names.join(", ") || "auth";
    notifySuccess(t("tryit.extractHarSuccess", { headers: headersText }));
  } catch (err) {
    notifyError(err, t("tryit.extractHarErr"));
  } finally {
    extractingFromHar.value = false;
  }
}

async function handleUploadStorageState(profile: AuthProfile, event: Event): Promise<void> {
  if (!projectId.value) return;
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;

  uploadingProfileId.value = profile.id;
  try {
    const updated = await uploadStorageState(projectId.value, profile.id, file);
    authProfiles.value = authProfiles.value.map((p) => (p.id === updated.id ? updated : p));
    notifySuccess(t("projects.toast.stateUploaded", { name: profile.name }));
  } catch (err) {
    notifyError(err, t("projects.err.uploadState"));
  } finally {
    uploadingProfileId.value = null;
    input.value = "";
  }
}

function launchRunForTarget(target: Target): void {
  void router.push({
    path: workspacePath("runs", target.project_id),
    query: { new: "1", targetId: target.id, targetUrl: target.base_url },
  });
}

async function handleDeleteCurrentProject(): Promise<void> {
  if (!projectId.value || deletingProject.value) return;
  deletingProject.value = true;
  try {
    await deleteProject(projectId.value, router);
  } catch (err) {
    notifyError(err, t("projects.err.deleteProject"));
  } finally {
    deletingProject.value = false;
  }
}

onMounted(() => {
  void loadDetails();
});
</script>

<template>
  <div class="space-y-5">
    <!-- Alerts -->
    <div
      v-if="bannerError"
      class="flex items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
    >
      <div class="flex items-center gap-2">
        <AlertTriangle class="h-4 w-4 shrink-0 text-rose-600" />
        <span>{{ bannerError }}</span>
      </div>
      <button type="button" class="text-rose-500 hover:text-rose-700" @click="bannerError = null">
        <X class="h-4 w-4" />
      </button>
    </div>

    <div
      v-if="bannerSuccess"
      class="flex items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800"
    >
      <div class="flex items-center gap-2">
        <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-600" />
        <span>{{ bannerSuccess }}</span>
      </div>
      <button
        type="button"
        class="text-emerald-500 hover:text-emerald-700"
        @click="bannerSuccess = null"
      >
        <X class="h-4 w-4" />
      </button>
    </div>

    <!-- Settings Tabs -->
    <div class="card flex flex-wrap items-center gap-2 p-3">
      <button
        type="button"
        data-testid="settings-tab-targets"
        class="inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-xs font-semibold transition"
        :class="
          activeTab === 'targets'
            ? 'bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-600/20'
            : 'text-slate-600 hover:bg-slate-100'
        "
        @click="activeTab = 'targets'"
      >
        <Globe class="h-3.5 w-3.5" />
        <span>{{ t("settings.tabTargets") }}</span>
        <span class="rounded-full bg-white px-1.5 py-0.5 text-[10px] shadow-sm">
          {{ targets.length }}
        </span>
      </button>

      <button
        type="button"
        data-testid="settings-tab-auth"
        class="inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-xs font-semibold transition"
        :class="
          activeTab === 'auth'
            ? 'bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-600/20'
            : 'text-slate-600 hover:bg-slate-100'
        "
        @click="activeTab = 'auth'"
      >
        <KeyRound class="h-3.5 w-3.5" />
        <span>{{ t("settings.tabCredentials") }}</span>
        <span class="rounded-full bg-white px-1.5 py-0.5 text-[10px] shadow-sm">
          {{ authProfiles.length }}
        </span>
      </button>

      <button
        type="button"
        data-testid="settings-tab-general"
        class="inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-xs font-semibold transition"
        :class="
          activeTab === 'general'
            ? 'bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-600/20'
            : 'text-slate-600 hover:bg-slate-100'
        "
        @click="activeTab = 'general'"
      >
        <Settings class="h-3.5 w-3.5" />
        <span>{{ t("settings.tabGeneral") }}</span>
      </button>
    </div>

    <!-- TAB 1: Targets -->
    <div v-if="activeTab === 'targets'" class="space-y-4">
      <form class="card p-5" @submit.prevent="handleAddTarget">
        <h3 class="text-sm font-semibold text-slate-900">{{ t("projects.target.formTitle") }}</h3>
        <p class="mt-0.5 text-xs text-slate-500">{{ t("projects.target.formDesc") }}</p>
        <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("settings.target.portalLabel") }}
            </label>
            <input
              v-model="targetPortalLabel"
              data-testid="target-portal-label-input"
              type="text"
              :placeholder="t('settings.target.portalPlaceholder')"
              class="input-field text-xs"
            />
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.target.rootDomain") }}
            </label>
            <input
              v-model="targetRootDomain"
              type="text"
              required
              placeholder="shop.example.com"
              class="input-field font-mono text-xs"
            />
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.target.baseUrl") }}
            </label>
            <input
              v-model="targetBaseUrl"
              type="url"
              required
              placeholder="https://api.example.com"
              class="input-field font-mono text-xs"
            />
          </div>
        </div>
        <div class="mt-4 flex justify-end">
          <button type="submit" class="btn-primary !text-xs" :disabled="addingTarget">
            <Plus class="h-3.5 w-3.5" />
            <span>{{ addingTarget ? t("projects.target.adding") : t("projects.target.addBtn") }}</span>
          </button>
        </div>
      </form>

      <div class="card overflow-hidden">
        <div class="card-header">
          <h3 class="text-sm font-semibold text-slate-900">{{ t("projects.target.listTitle") }}</h3>
        </div>
        <div v-if="loadingDetails" class="p-6 text-center text-xs text-slate-500">
          {{ t("projects.target.loading") }}
        </div>
        <div v-else-if="targets.length === 0" class="p-6">
          <EmptyState
            :title="t('projects.target.emptyTitle')"
            :description="t('projects.target.emptyDesc')"
          />
        </div>
        <div v-else class="divide-y divide-slate-100">
          <div
            v-for="target in targets"
            :key="target.id"
            class="flex flex-col justify-between gap-3 px-5 py-4 sm:flex-row sm:items-center"
          >
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <Globe class="h-4 w-4 shrink-0 text-indigo-500" />
                <span
                  v-if="getTargetPortalLabel(target)"
                  data-testid="target-portal-badge"
                  class="rounded-md bg-indigo-50 px-2 py-0.5 text-xs font-semibold text-indigo-700 ring-1 ring-inset ring-indigo-600/20"
                >
                  {{ getTargetPortalLabel(target) }}
                </span>
                <span class="truncate font-mono text-sm font-semibold text-slate-900">
                  {{ target.root_domain }}
                </span>
              </div>
              <p class="mt-0.5 truncate font-mono text-xs text-slate-500">{{ target.base_url }}</p>
            </div>
            <div class="flex shrink-0 items-center gap-2">
              <button
                type="button"
                class="btn-secondary !px-2.5 !py-1.5 !text-xs"
                @click="launchRunForTarget(target)"
              >
                <PlayCircle class="h-3.5 w-3.5 text-indigo-600" />
                <span>{{ t("projects.target.runBtn") }}</span>
              </button>
              <button
                type="button"
                class="btn-danger !px-2.5 !py-1.5 !text-xs"
                :title="t('projects.target.deleteTitle')"
                @click="handleDeleteTarget(target.id)"
              >
                <Trash2 class="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 2: Credentials -->
    <div v-else-if="activeTab === 'auth'" class="space-y-4">
      <div class="rounded-xl border border-indigo-200/80 bg-indigo-50/50 p-4 text-xs text-indigo-950">
        <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
          <div class="flex items-start gap-2.5">
            <ShieldCheck class="mt-0.5 h-4 w-4 shrink-0 text-indigo-600" />
            <div class="space-y-1">
              <p class="font-semibold">{{ t("projects.auth.hygieneTitle") }}</p>
              <p class="leading-relaxed text-indigo-900/80">
                {{
                  t("projects.auth.hygieneDesc", {
                    envExample: "ENV:RECONQA_ACCOUNT_A_TOKEN",
                    fileExample: "storage_state.json",
                  })
                }}
              </p>
            </div>
          </div>
          <div class="flex shrink-0 items-center gap-2">
            <span
              class="inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-medium ring-1 ring-inset"
              :class="
                hasAccountA
                  ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
                  : 'bg-white text-slate-500 ring-slate-300'
              "
            >
              {{ hasAccountA ? "✓" : "○" }} account_a
            </span>
            <span
              class="inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-medium ring-1 ring-inset"
              :class="
                hasAccountB
                  ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
                  : 'bg-white text-slate-500 ring-slate-300'
              "
            >
              {{ hasAccountB ? "✓" : "○" }} account_b
            </span>
          </div>
        </div>
      </div>

      <form class="card p-5" @submit.prevent="handleAddAuthProfile">
        <h3 class="text-sm font-semibold text-slate-900">{{ t("projects.auth.formTitle") }}</h3>
        <p class="mt-0.5 text-xs text-slate-500">{{ t("projects.auth.formDesc") }}</p>
        <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.auth.nameLabel") }}
            </label>
            <input
              v-model="profileName"
              type="text"
              required
              :placeholder="t('projects.auth.namePlaceholder')"
              class="input-field text-xs"
            />
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.auth.roleLabel") }}
            </label>
            <input
              v-model="profileRole"
              data-testid="auth-role-input"
              type="text"
              required
              :placeholder="t('settings.auth.customRolePlaceholder')"
              class="input-field font-mono text-xs"
            />
            <div class="mt-1.5 flex flex-wrap items-center gap-1">
              <button
                v-for="rolePreset in ROLE_PRESETS"
                :key="rolePreset"
                type="button"
                class="rounded px-1.5 py-0.5 font-mono text-[10px] font-medium transition"
                :class="
                  profileRole === rolePreset
                    ? 'bg-indigo-600 text-white shadow-sm'
                    : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                "
                @click="profileRole = rolePreset"
              >
                {{ rolePreset }}
              </button>
            </div>
            <p class="mt-1 text-[10px] text-slate-400">{{ t("settings.auth.rolePresetsHint") }}</p>
          </div>
        </div>

        <div class="mt-4">
          <div class="flex items-center justify-between">
            <label class="text-xs font-medium text-slate-700">
              {{ t("projects.auth.headersLabel") }}
            </label>
            <button
              type="button"
              class="text-xs font-medium text-indigo-600 hover:text-indigo-800"
              @click="headerRefRows.push({ header: '', envRef: '' })"
            >
              {{ t("projects.auth.addHeaderBtn") }}
            </button>
          </div>

          <div class="mt-2 space-y-2">
            <div v-for="(row, idx) in headerRefRows" :key="idx" class="flex items-center gap-2">
              <input
                v-model="row.header"
                type="text"
                :placeholder="t('projects.auth.headerPlaceholder')"
                class="input-field !py-1.5 font-mono text-xs"
              />
              <span class="text-xs text-slate-400">→</span>
              <input
                v-model="row.envRef"
                type="text"
                :placeholder="t('settings.auth.valuePlaceholder')"
                class="input-field !py-1.5 font-mono text-xs"
              />
              <button
                type="button"
                class="rounded p-1.5 text-slate-400 hover:bg-slate-100 hover:text-rose-600"
                :title="t('projects.auth.removeHeaderTitle')"
                @click="headerRefRows.splice(idx, 1)"
              >
                <X class="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
          <p class="mt-2 text-[11px] leading-relaxed text-slate-500">
            {{ t("settings.auth.valueSupportHint") }}
          </p>
        </div>

        <div class="mt-4 flex justify-end">
          <button type="submit" class="btn-primary !text-xs" :disabled="addingProfile">
            <Plus class="h-3.5 w-3.5" />
            <span>{{ addingProfile ? t("projects.auth.creating") : t("projects.auth.createBtn") }}</span>
          </button>
        </div>
      </form>

      <div class="card overflow-hidden">
        <div class="card-header">
          <h3 class="text-sm font-semibold text-slate-900">{{ t("projects.auth.listTitle") }}</h3>
          <button
            type="button"
            data-testid="extract-har-cred-btn"
            class="btn-secondary !py-1.5 !text-xs"
            :disabled="extractingFromHar || !projectId"
            @click="handleExtractFromHar"
          >
            <Loader2 v-if="extractingFromHar" class="h-3.5 w-3.5 animate-spin text-indigo-600" />
            <Sparkles v-else class="h-3.5 w-3.5 text-indigo-600" />
            <span>{{ extractingFromHar ? t("tryit.extractingHarBtn") : t("tryit.extractHarBtn") }}</span>
          </button>
        </div>
        <div v-if="loadingDetails" class="p-6 text-center text-xs text-slate-500">
          {{ t("projects.auth.loading") }}
        </div>
        <div v-else-if="authProfiles.length === 0" class="p-6">
          <EmptyState
            :title="t('projects.auth.emptyTitle')"
            :description="t('projects.auth.emptyDesc')"
          />
        </div>
        <div v-else class="divide-y divide-slate-100">
          <div v-for="profile in authProfiles" :key="profile.id" class="space-y-3 px-5 py-4">
            <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-sm font-semibold text-slate-900">{{ profile.name }}</span>
                  <StatusBadge :status="profile.role_tag" />
                </div>
                <p class="mt-0.5 font-mono text-[11px] text-slate-400">ID: {{ profile.id }}</p>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <label
                  class="btn-secondary cursor-pointer !px-2.5 !py-1.5 !text-xs"
                  :class="uploadingProfileId === profile.id ? 'opacity-60' : ''"
                >
                  <Upload class="h-3.5 w-3.5 text-indigo-600" />
                  <span>
                    {{
                      uploadingProfileId === profile.id
                        ? t("projects.auth.uploading")
                        : profile.storage_state_path
                          ? t("projects.auth.replaceState")
                          : t("projects.auth.uploadState")
                    }}
                  </span>
                  <input
                    type="file"
                    accept=".json,application/json"
                    class="hidden"
                    :disabled="uploadingProfileId === profile.id"
                    @change="handleUploadStorageState(profile, $event)"
                  />
                </label>
                <button
                  type="button"
                  class="btn-danger !px-2.5 !py-1.5 !text-xs"
                  :title="t('projects.auth.deleteTitle')"
                  @click="handleDeleteAuthProfile(profile.id)"
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>

            <div class="flex flex-wrap items-center gap-2 text-xs">
              <span
                class="inline-flex items-center gap-1.5 rounded-md px-2 py-1 font-mono text-[11px]"
                :class="
                  profile.storage_state_path
                    ? 'bg-emerald-50 text-emerald-800 ring-1 ring-inset ring-emerald-600/20'
                    : 'bg-slate-100 text-slate-500'
                "
              >
                <span>storage_state:</span>
                <span>
                  {{
                    profile.storage_state_path
                      ? storageStateBasename(profile.storage_state_path)
                      : t("projects.auth.notUploaded")
                  }}
                </span>
              </span>
              <span
                v-for="(envVal, headerKey) in profile.extra_headers_ref"
                :key="headerKey"
                class="inline-flex items-center gap-1 rounded-md bg-slate-100 px-2 py-1 font-mono text-[11px] text-slate-700"
              >
                <span class="font-semibold">{{ headerKey }}</span>
                <span class="text-slate-400">→</span>
                <span data-testid="masked-header-value">{{ maskHeaderValue(envVal) }}</span>
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 3: General & Danger Zone -->
    <div v-else class="space-y-5">
      <form class="card space-y-4 p-5" @submit.prevent="handleSaveProjectMeta">
        <div class="flex items-center justify-between gap-3">
          <h3 class="text-sm font-semibold text-slate-900">{{ t("settings.generalTitle") }}</h3>
          <button
            type="submit"
            data-testid="project-save-btn"
            class="btn-primary !py-1.5 !text-xs"
            :disabled="savingProject || !editProjectName.trim()"
          >
            <Loader2 v-if="savingProject" class="h-3.5 w-3.5 animate-spin" />
            <Save v-else class="h-3.5 w-3.5" />
            <span>{{ savingProject ? t("settings.savingBtn") : t("settings.saveBtn") }}</span>
          </button>
        </div>

        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div class="sm:col-span-2">
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("settings.nameLabel") }} *
            </label>
            <input
              v-model="editProjectName"
              data-testid="project-name-input"
              type="text"
              required
              maxlength="120"
              :placeholder="t('projects.modal.namePlaceholder')"
              class="input-field text-sm font-medium"
            />
          </div>

          <div class="sm:col-span-2">
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("settings.descLabel") }}
            </label>
            <textarea
              v-model="editProjectDesc"
              data-testid="project-desc-input"
              rows="3"
              maxlength="2000"
              :placeholder="t('projects.modal.descPlaceholder')"
              class="input-field text-xs leading-relaxed"
            />
          </div>
        </div>

        <dl class="grid grid-cols-1 gap-4 border-t border-slate-100 pt-4 text-xs sm:grid-cols-3">
          <div>
            <dt class="font-medium text-slate-500">{{ t("settings.slugLabel") }}</dt>
            <dd class="mt-1 font-mono text-slate-800">{{ activeProject?.slug || "—" }}</dd>
          </div>
          <div>
            <dt class="font-medium text-slate-500">{{ t("settings.idLabel") }}</dt>
            <dd class="mt-1 font-mono text-slate-600">{{ projectId || "—" }}</dd>
          </div>
          <div>
            <dt class="font-medium text-slate-500">{{ t("settings.createdLabel") }}</dt>
            <dd class="mt-1 text-slate-700">{{ formatDate(activeProject?.created_at) }}</dd>
          </div>
        </dl>
      </form>

      <div class="rounded-xl border border-rose-200 bg-rose-50/50 p-5">
        <h3 class="text-sm font-semibold text-rose-800">{{ t("settings.dangerTitle") }}</h3>
        <p class="mt-1 text-xs leading-relaxed text-rose-700">{{ t("settings.dangerDesc") }}</p>
        <div class="mt-4 flex items-center gap-2">
          <template v-if="confirmingDeleteProject">
            <button
              type="button"
              class="rounded-lg bg-rose-600 px-3.5 py-2 text-xs font-semibold text-white hover:bg-rose-700 disabled:opacity-50"
              :disabled="deletingProject"
              @click="handleDeleteCurrentProject"
            >
              {{ t("settings.confirmDeleteBtn") }}
            </button>
            <button
              type="button"
              class="btn-secondary !py-1.5 !text-xs"
              @click="confirmingDeleteProject = false"
            >
              {{ t("common.cancel") }}
            </button>
          </template>
          <button
            v-else
            type="button"
            class="btn-danger !text-xs"
            @click="confirmingDeleteProject = true"
          >
            <Trash2 class="h-3.5 w-3.5" />
            <span>{{ t("settings.deleteProjectBtn") }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
