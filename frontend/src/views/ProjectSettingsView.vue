<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardPaste,
  Eye,
  EyeOff,
  Globe,
  KeyRound,
  Loader2,
  Pencil,
  PlayCircle,
  Plus,
  Save,
  Settings,
  ShieldCheck,
  Sparkles,
  Trash2,
  Upload,
  X,
  Zap,
} from "lucide-vue-next";
import {
  createAuthProfile,
  createTarget,
  deleteAuthProfile,
  deleteTarget,
  discoverProjectPortals,
  extractCredentialFromHar,
  listAuthProfiles,
  listTargets,
  testAuthProfileLogin,
  updateTarget,
  uploadStorageState,
} from "@/api/projects";
import type { AuthProfile, AuthType, DiscoveredPortal, RoleTag, Target } from "@/api/types";
import { parseAuthPaste } from "@/utils/authPasteParser";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import EmptyState from "@/components/layout/EmptyState.vue";
import DiscoveredPortalsPanel from "@/components/workspace/DiscoveredPortalsPanel.vue";
import { useWorkspace } from "@/stores/workspace";
import { useI18n } from "@/i18n";
import "@/i18n/locales/explore";

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

// Targets form & inline edit state
const addingTarget = ref(false);
const targetPortalLabel = ref("");
const targetRootDomain = ref("");
const targetBaseUrl = ref("");
const editingTargetId = ref<string | null>(null);
const editTargetPortalLabel = ref("");
const editTargetRootDomain = ref("");
const editTargetBaseUrl = ref("");
const savingTarget = ref(false);
const discoveringPortals = ref(false);
const discoveredPortals = ref<DiscoveredPortal[] | null>(null);
const discoveredRootDomain = ref("");

// Credentials form
const ROLE_PRESETS = ["admin", "pharmacy", "doctor", "assistant", "account_a", "account_b", "guest"] as const;
const PROFILE_AUTH_TYPES: ReadonlyArray<AuthType> = ["headers", "basic", "login_api", "form_login"];
const addingProfile = ref(false);
const profileName = ref("");
const profileRole = ref<RoleTag>("account_a");
const profileAuthType = ref<AuthType>("headers");
const headerRefRows = ref<HeaderRefRow[]>([
  { header: "Authorization", envRef: "ENV:RECONQA_ACCOUNT_A_TOKEN" },
]);
// Extended login_config fields
const basicUsername = ref("");
const basicPassword = ref("");
const basicVerifyUrl = ref("");
const showBasicPassword = ref(false);
const loginApiUrl = ref("");
const loginApiMethod = ref<"POST" | "PUT" | "PATCH">("POST");
const loginApiContentType = ref<"json" | "form">("json");
const loginApiUsernameField = ref("username");
const loginApiPasswordField = ref("password");
const loginApiUsername = ref("");
const loginApiPassword = ref("");
const showLoginApiPassword = ref(false);
const loginApiTokenJsonpath = ref("");
const loginApiTokenHeader = ref("Authorization");
const loginApiTokenPrefix = ref("Bearer ");
const loginApiVerifyUrl = ref("");
const loginApiExtraFields = ref<Record<string, unknown> | undefined>(undefined);
const formLoginUrl = ref("");
const formLoginUsername = ref("");
const formLoginPassword = ref("");
const showFormLoginPassword = ref(false);
const formLoginVerifyUrl = ref("");
const formUsernameSelector = ref("");
const formPasswordSelector = ref("");
const formSubmitSelector = ref("");
// Burp-style Smart Paste in Settings
const settingsSmartPasteOpen = ref(false);
const settingsSmartPasteText = ref("");
const settingsSmartPasteNotice = ref<string | null>(null);
const settingsSmartPasteError = ref<string | null>(null);

const uploadingProfileId = ref<string | null>(null);
const testingProfileId = ref<string | null>(null);
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
  if (/^env:/i.test(trimmed) || /^\$\{.+\}$/.test(trimmed)) return trimmed;
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
  discoveredPortals.value = null;
  profileName.value = "";
  profileRole.value = "account_a";
  profileAuthType.value = "headers";
  headerRefRows.value = [{ header: "Authorization", envRef: "ENV:RECONQA_ACCOUNT_A_TOKEN" }];
  basicUsername.value = "";
  basicPassword.value = "";
  basicVerifyUrl.value = "";
  showBasicPassword.value = false;
  loginApiUrl.value = "";
  loginApiMethod.value = "POST";
  loginApiContentType.value = "json";
  loginApiUsernameField.value = "username";
  loginApiPasswordField.value = "password";
  loginApiUsername.value = "";
  loginApiPassword.value = "";
  showLoginApiPassword.value = false;
  loginApiTokenJsonpath.value = "";
  loginApiTokenHeader.value = "Authorization";
  loginApiTokenPrefix.value = "Bearer ";
  loginApiVerifyUrl.value = "";
  loginApiExtraFields.value = undefined;
  formLoginUrl.value = "";
  formLoginUsername.value = "";
  formLoginPassword.value = "";
  showFormLoginPassword.value = false;
  formLoginVerifyUrl.value = "";
  formUsernameSelector.value = "";
  formPasswordSelector.value = "";
  formSubmitSelector.value = "";
  settingsSmartPasteOpen.value = false;
  settingsSmartPasteText.value = "";
  settingsSmartPasteNotice.value = null;
  settingsSmartPasteError.value = null;
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

function startEditTarget(target: Target): void {
  editingTargetId.value = target.id;
  editTargetPortalLabel.value = getTargetPortalLabel(target);
  editTargetRootDomain.value = target.root_domain;
  editTargetBaseUrl.value = target.base_url;
}

function cancelEditTarget(): void {
  editingTargetId.value = null;
}

async function handleSaveTargetEdit(target: Target): Promise<void> {
  if (!projectId.value || savingTarget.value) return;
  savingTarget.value = true;
  try {
    const nextRules = { ...(target.scope_rules ?? {}) };
    const trimmedLabel = editTargetPortalLabel.value.trim();
    if (trimmedLabel) nextRules.portal_label = trimmedLabel;
    else delete nextRules.portal_label;

    const updated = await updateTarget(projectId.value, target.id, {
      root_domain: editTargetRootDomain.value.trim() || target.root_domain,
      base_url: editTargetBaseUrl.value.trim() || target.base_url,
      scope_rules: nextRules,
    });
    targets.value = targets.value.map((item) => (item.id === updated.id ? updated : item));
    editingTargetId.value = null;
    notifySuccess(t("settings.target.updatedToast"));
  } catch (err) {
    notifyError(err, t("settings.target.errUpdate"));
  } finally {
    savingTarget.value = false;
  }
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

async function handleDiscoverPortals(): Promise<void> {
  if (!projectId.value || discoveringPortals.value) return;
  discoveringPortals.value = true;
  bannerError.value = null;
  try {
    const res = await discoverProjectPortals(projectId.value);
    discoveredPortals.value = res.portals ?? [];
    discoveredRootDomain.value = res.root_domain || "";
  } catch (err) {
    notifyError(err, t("settings.target.errDiscover"));
  } finally {
    discoveringPortals.value = false;
  }
}

function onSettingsPortalsAdded(added: Target[], skipped: number): void {
  if (added.length > 0) {
    const existingIds = new Set(targets.value.map((tItem) => tItem.id));
    targets.value = [...targets.value, ...added.filter((a) => !existingIds.has(a.id))];
  }
  notifySuccess(t("settings.target.batchAddedToast", { added: added.length, skipped }));
}

function applyHeaderPreset(preset: "bearer" | "cookie"): void {
  if (preset === "bearer") {
    const idx = headerRefRows.value.findIndex(
      (r) => r.header.trim().toLowerCase() === "authorization"
    );
    const envName =
      profileRole.value === "account_b"
        ? "ENV:RECONQA_ACCOUNT_B_TOKEN"
        : "ENV:RECONQA_ACCOUNT_A_TOKEN";
    if (idx >= 0) {
      headerRefRows.value[idx] = { header: "Authorization", envRef: envName };
    } else {
      headerRefRows.value.push({ header: "Authorization", envRef: envName });
    }
  } else {
    const idx = headerRefRows.value.findIndex(
      (r) => r.header.trim().toLowerCase() === "cookie"
    );
    if (idx >= 0) {
      headerRefRows.value[idx] = { header: "Cookie", envRef: "sid=...; token=..." };
    } else {
      headerRefRows.value.push({ header: "Cookie", envRef: "sid=...; token=..." });
    }
  }
}

function normalizeLoginMethod(method: string | undefined): "POST" | "PUT" | "PATCH" {
  const upper = (method || "POST").toUpperCase();
  if (upper === "PUT" || upper === "PATCH") return upper;
  return "POST";
}

function handleSettingsSmartPaste(): void {
  settingsSmartPasteError.value = null;
  settingsSmartPasteNotice.value = null;
  const parsed = parseAuthPaste(settingsSmartPasteText.value);
  if (!parsed.ok) {
    settingsSmartPasteError.value =
      parsed.errorKey === "empty"
        ? t("explore.auth.smartPasteErrEmpty")
        : t("explore.auth.smartPasteErrNone");
    return;
  }

  if (parsed.mode === "login_api") {
    profileAuthType.value = "login_api";
    loginApiUrl.value = parsed.loginUrl ?? "";
    loginApiMethod.value = normalizeLoginMethod(parsed.loginMethod);
    loginApiContentType.value = parsed.loginContentType ?? "json";
    loginApiUsernameField.value = parsed.usernameField ?? "username";
    loginApiPasswordField.value = parsed.passwordField ?? "password";
    loginApiUsername.value = parsed.username ?? "";
    loginApiPassword.value = parsed.password ?? "";
    loginApiExtraFields.value = parsed.extraFields;
    settingsSmartPasteNotice.value = t("explore.auth.smartPasteExtractedLogin", {
      method: loginApiMethod.value,
      url: parsed.loginUrl || "",
    });
  } else {
    loginApiExtraFields.value = undefined;
    loginApiMethod.value = "POST";
    if (parsed.mode === "basic") {
      profileAuthType.value = "basic";
      basicUsername.value = parsed.username ?? "";
      basicPassword.value = parsed.password ?? "";
      if (parsed.detectedUrl && !basicVerifyUrl.value.trim()) {
        basicVerifyUrl.value = parsed.detectedUrl;
      }
      settingsSmartPasteNotice.value = t("explore.auth.smartPasteExtractedBasic", {
        user: parsed.username ?? "",
      });
    } else if (parsed.mode === "bearer") {
      profileAuthType.value = "headers";
      const hdrName = parsed.tokenHeader || "Authorization";
      const fullVal = `${parsed.tokenPrefix ?? "Bearer "}${parsed.token ?? ""}`;
      headerRefRows.value = [{ header: hdrName, envRef: fullVal }];
      settingsSmartPasteNotice.value = t("explore.auth.smartPasteExtractedBearer", {
        header: hdrName,
      });
    } else if (parsed.mode === "cookie") {
      profileAuthType.value = "headers";
      headerRefRows.value = [{ header: "Cookie", envRef: parsed.cookie ?? "" }];
      settingsSmartPasteNotice.value = t("explore.auth.smartPasteExtractedCookie");
    } else if (parsed.mode === "headers" && parsed.headers) {
      profileAuthType.value = "headers";
      headerRefRows.value = Object.entries(parsed.headers).map(([header, envRef]) => ({
        header,
        envRef,
      }));
      settingsSmartPasteNotice.value = t("explore.auth.smartPasteExtractedHeaders", {
        count: parsed.headerNames.length,
        headers: parsed.headerNames.join(", "),
      });
    }
  }

  settingsSmartPasteText.value = "";
  settingsSmartPasteOpen.value = false;
}

function profileAuthTypeLabel(profile: AuthProfile): string {
  return (profile.auth_type || "headers").toLowerCase();
}

function profileAuthTypeBadgeClass(authType: string): string {
  switch (authType) {
    case "login_api":
      return "bg-violet-50 text-violet-700 ring-violet-600/20";
    case "form_login":
      return "bg-sky-50 text-sky-700 ring-sky-600/20";
    case "basic":
      return "bg-amber-50 text-amber-800 ring-amber-600/20";
    default:
      return "bg-slate-100 text-slate-700 ring-slate-500/20";
  }
}

function canTestProfileLogin(profile: AuthProfile): boolean {
  const kind = profileAuthTypeLabel(profile);
  return kind === "login_api" || kind === "form_login" || kind === "basic";
}

function formatRedactedPassword(raw: unknown): string {
  if (typeof raw !== "string" || !raw.trim()) return "";
  const trimmed = raw.trim();
  if (/^env:/i.test(trimmed) || /^\$\{.+\}$/.test(trimmed)) return trimmed;
  return "••••";
}

async function handleAddAuthProfile(): Promise<void> {
  if (!projectId.value || !profileName.value.trim()) return;
  addingProfile.value = true;
  try {
    const authType = profileAuthType.value;
    const extraHeadersRef: Record<string, string> = {};
    let loginConfig: Record<string, unknown> | undefined;

    if (authType === "headers") {
      for (const row of headerRefRows.value) {
        const k = row.header.trim();
        const v = row.envRef.trim();
        if (k && v) extraHeadersRef[k] = v;
      }
    } else if (authType === "basic") {
      loginConfig = {
        username: basicUsername.value.trim(),
        password: basicPassword.value,
        ...(basicVerifyUrl.value.trim() ? { verify_url: basicVerifyUrl.value.trim() } : {}),
      };
    } else if (authType === "login_api") {
      loginConfig = {
        login_url: loginApiUrl.value.trim(),
        login_method: loginApiMethod.value,
        login_content_type: loginApiContentType.value,
        username_field: loginApiUsernameField.value.trim() || "username",
        password_field: loginApiPasswordField.value.trim() || "password",
        username: loginApiUsername.value.trim(),
        password: loginApiPassword.value,
        token_header: loginApiTokenHeader.value.trim() || "Authorization",
        token_prefix: loginApiTokenPrefix.value,
        ...(loginApiTokenJsonpath.value.trim()
          ? { token_jsonpath: loginApiTokenJsonpath.value.trim() }
          : {}),
        ...(loginApiVerifyUrl.value.trim() ? { verify_url: loginApiVerifyUrl.value.trim() } : {}),
        ...(loginApiExtraFields.value && Object.keys(loginApiExtraFields.value).length > 0
          ? { extra_fields: loginApiExtraFields.value }
          : {}),
      };
    } else if (authType === "form_login") {
      loginConfig = {
        login_url: formLoginUrl.value.trim(),
        username: formLoginUsername.value.trim(),
        password: formLoginPassword.value,
        ...(formLoginVerifyUrl.value.trim() ? { verify_url: formLoginVerifyUrl.value.trim() } : {}),
        ...(formUsernameSelector.value.trim()
          ? { username_selector: formUsernameSelector.value.trim() }
          : {}),
        ...(formPasswordSelector.value.trim()
          ? { password_selector: formPasswordSelector.value.trim() }
          : {}),
        ...(formSubmitSelector.value.trim()
          ? { submit_selector: formSubmitSelector.value.trim() }
          : {}),
      };
    }

    const created = await createAuthProfile(projectId.value, {
      name: profileName.value.trim(),
      role_tag: profileRole.value,
      auth_type: authType,
      extra_headers_ref: extraHeadersRef,
      login_config: loginConfig,
    });
    authProfiles.value = [...authProfiles.value, created];
    profileName.value = "";
    basicPassword.value = "";
    loginApiPassword.value = "";
    formLoginPassword.value = "";
    loginApiMethod.value = "POST";
    loginApiExtraFields.value = undefined;
    settingsSmartPasteText.value = "";
    settingsSmartPasteNotice.value = null;
    settingsSmartPasteError.value = null;
    notifySuccess(
      t("projects.toast.profileCreated", { name: created.name, role: created.role_tag })
    );
  } catch (err) {
    notifyError(err, t("projects.err.createProfile"));
  } finally {
    addingProfile.value = false;
  }
}

async function handleTestProfileLogin(profile: AuthProfile): Promise<void> {
  if (!projectId.value || testingProfileId.value) return;
  testingProfileId.value = profile.id;
  try {
    const res = await testAuthProfileLogin(projectId.value, profile.id);
    if (res.profile) {
      authProfiles.value = authProfiles.value.map((p) =>
        p.id === res.profile.id ? res.profile : p
      );
    }
    if (res.ok) {
      const headersText = (res.header_names || []).join(", ");
      notifySuccess(
        headersText
          ? t("settings.auth.testLoginOk", { message: res.message, headers: headersText })
          : t("settings.auth.testLoginOkNoHeaders", { message: res.message })
      );
    } else {
      notifyError(
        new Error(t("settings.auth.testLoginFail", { message: res.message })),
        t("settings.auth.testLoginErr")
      );
    }
  } catch (err) {
    notifyError(err, t("settings.auth.testLoginErr"));
  } finally {
    testingProfileId.value = null;
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
          <button
            type="button"
            data-testid="settings-discover-portals-btn"
            class="btn-secondary !px-3 !py-1.5 !text-xs"
            :disabled="discoveringPortals || !projectId"
            @click="handleDiscoverPortals"
          >
            <Loader2 v-if="discoveringPortals" class="h-3.5 w-3.5 animate-spin text-emerald-600" />
            <Sparkles v-else class="h-3.5 w-3.5 text-emerald-600" />
            <span>
              {{
                discoveringPortals
                  ? t("settings.target.discoveringBtn")
                  : t("settings.target.discoverBtn")
              }}
            </span>
          </button>
        </div>

        <div v-if="discoveredPortals !== null && projectId" class="border-b border-slate-100 p-4">
          <DiscoveredPortalsPanel
            :project-id="projectId"
            :portals="discoveredPortals"
            :existing-targets="targets"
            :root-domain="discoveredRootDomain"
            @added="onSettingsPortalsAdded"
            @error="(msg) => notifyError(new Error(msg), msg)"
          />
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
            class="px-5 py-4"
          >
            <form
              v-if="editingTargetId === target.id"
              data-testid="edit-target-form"
              class="grid grid-cols-1 gap-3 sm:grid-cols-12 sm:items-end"
              @submit.prevent="handleSaveTargetEdit(target)"
            >
              <div class="sm:col-span-3">
                <label class="mb-1 block text-[11px] font-medium text-slate-600">
                  {{ t("settings.target.portalLabel") }}
                </label>
                <input
                  v-model="editTargetPortalLabel"
                  data-testid="edit-target-portal-input"
                  type="text"
                  :placeholder="t('settings.target.portalPlaceholder')"
                  class="input-field !py-1.5 text-xs"
                />
              </div>
              <div class="sm:col-span-3">
                <label class="mb-1 block text-[11px] font-medium text-slate-600">
                  {{ t("projects.target.rootDomain") }}
                </label>
                <input
                  v-model="editTargetRootDomain"
                  type="text"
                  required
                  class="input-field !py-1.5 font-mono text-xs"
                />
              </div>
              <div class="sm:col-span-4">
                <label class="mb-1 block text-[11px] font-medium text-slate-600">
                  {{ t("projects.target.baseUrl") }}
                </label>
                <input
                  v-model="editTargetBaseUrl"
                  type="url"
                  required
                  class="input-field !py-1.5 font-mono text-xs"
                />
              </div>
              <div class="flex items-center justify-end gap-1.5 sm:col-span-2">
                <button
                  type="submit"
                  data-testid="edit-target-save-btn"
                  class="btn-primary !px-2.5 !py-1.5 !text-xs"
                  :disabled="savingTarget"
                >
                  {{ t("settings.target.saveBtn") }}
                </button>
                <button
                  type="button"
                  class="btn-secondary !px-2 !py-1.5 !text-xs"
                  @click="cancelEditTarget"
                >
                  {{ t("common.cancel") }}
                </button>
              </div>
            </form>

            <div
              v-else
              class="flex flex-col justify-between gap-3 sm:flex-row sm:items-center"
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
                  data-testid="edit-target-btn"
                  class="btn-secondary !px-2.5 !py-1.5 !text-xs"
                  @click="startEditTarget(target)"
                >
                  <Pencil class="h-3.5 w-3.5 text-slate-500" />
                  <span>{{ t("settings.target.editBtn") }}</span>
                </button>
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
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 class="text-sm font-semibold text-slate-900">{{ t("projects.auth.formTitle") }}</h3>
            <p class="mt-0.5 text-xs text-slate-500">{{ t("projects.auth.formDesc") }}</p>
          </div>
          <button
            type="button"
            data-testid="settings-smart-paste-toggle"
            class="inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50/70 px-3 py-1.5 text-xs font-medium text-indigo-700 transition hover:bg-indigo-100"
            @click="settingsSmartPasteOpen = !settingsSmartPasteOpen"
          >
            <ClipboardPaste class="h-3.5 w-3.5" />
            <span>{{ t("settings.auth.smartPasteBtn") }}</span>
          </button>
        </div>

        <!-- Burp-Style Smart Paste Inline Box -->
        <div
          v-if="settingsSmartPasteOpen"
          class="mt-3 space-y-2 rounded-xl border border-indigo-200 bg-indigo-50/40 p-3.5"
        >
          <p class="text-[11px] text-indigo-900">{{ t("settings.auth.smartPasteHint") }}</p>
          <textarea
            v-model="settingsSmartPasteText"
            data-testid="settings-smart-paste-input"
            rows="3"
            class="input-field bg-white font-mono text-[11px] leading-relaxed"
            :placeholder="t('explore.auth.smartPastePlaceholder')"
          />
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p v-if="settingsSmartPasteError" class="text-[11px] font-medium text-rose-600">
              {{ settingsSmartPasteError }}
            </p>
            <span v-else />
            <button
              type="button"
              data-testid="settings-smart-paste-apply"
              class="btn-primary !px-3 !py-1 !text-xs"
              @click="handleSettingsSmartPaste"
            >
              <Sparkles class="h-3 w-3" />
              <span>{{ t("settings.auth.smartPasteApply") }}</span>
            </button>
          </div>
        </div>

        <div
          v-if="settingsSmartPasteNotice"
          class="mt-3 flex items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs text-emerald-800"
        >
          <span class="flex items-center gap-1.5">
            <CheckCircle2 class="h-3.5 w-3.5 shrink-0 text-emerald-600" />
            <span>{{ settingsSmartPasteNotice }}</span>
          </span>
          <button
            type="button"
            class="text-emerald-600 hover:text-emerald-800"
            @click="settingsSmartPasteNotice = null"
          >
            <X class="h-3.5 w-3.5" />
          </button>
        </div>

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

        <!-- Auth Type Selector -->
        <div class="mt-4" data-testid="auth-type-selector">
          <label class="mb-1.5 block text-xs font-medium text-slate-700">
            {{ t("settings.auth.typeLabel") }}
          </label>
          <div class="flex flex-wrap items-center gap-2">
            <button
              v-for="aType in PROFILE_AUTH_TYPES"
              :key="aType"
              type="button"
              :data-testid="`auth-type-opt-${aType}`"
              class="rounded-lg px-3 py-1.5 text-xs font-medium transition"
              :class="
                profileAuthType === aType
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
              "
              @click="profileAuthType = aType"
            >
              {{ t(`settings.auth.type.${aType}`) }}
            </button>
          </div>
        </div>

        <!-- Auth Type: headers -->
        <div v-if="profileAuthType === 'headers'" class="mt-4">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <label class="text-xs font-medium text-slate-700">
              {{ t("projects.auth.headersLabel") }}
            </label>
            <div class="flex flex-wrap items-center gap-2">
              <button
                type="button"
                class="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600 hover:bg-indigo-50 hover:text-indigo-700"
                @click="applyHeaderPreset('bearer')"
              >
                {{ t("settings.auth.presetBearer") }}
              </button>
              <button
                type="button"
                class="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600 hover:bg-indigo-50 hover:text-indigo-700"
                @click="applyHeaderPreset('cookie')"
              >
                {{ t("settings.auth.presetCookie") }}
              </button>
              <button
                type="button"
                class="text-xs font-medium text-indigo-600 hover:text-indigo-800"
                @click="headerRefRows.push({ header: '', envRef: '' })"
              >
                {{ t("projects.auth.addHeaderBtn") }}
              </button>
            </div>
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

        <!-- Auth Type: basic -->
        <div v-else-if="profileAuthType === 'basic'" class="mt-4 space-y-3">
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.usernameLabel") }}
              </label>
              <input
                v-model="basicUsername"
                type="text"
                required
                :placeholder="t('settings.auth.usernamePlaceholder')"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.passwordLabel") }}
              </label>
              <div class="relative">
                <input
                  v-model="basicPassword"
                  :type="showBasicPassword ? 'text' : 'password'"
                  required
                  :placeholder="t('settings.auth.passwordPlaceholder')"
                  class="input-field !py-1.5 pr-8 font-mono text-xs"
                />
                <button
                  type="button"
                  class="absolute right-2 top-1.5 text-slate-400 hover:text-slate-600"
                  @click="showBasicPassword = !showBasicPassword"
                >
                  <EyeOff v-if="showBasicPassword" class="h-3.5 w-3.5" />
                  <Eye v-else class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("settings.auth.verifyUrlLabel") }}
            </label>
            <input
              v-model="basicVerifyUrl"
              type="url"
              :placeholder="t('settings.auth.verifyUrlPlaceholder')"
              class="input-field !py-1.5 font-mono text-xs"
            />
          </div>
          <p class="text-[11px] text-slate-500">{{ t("settings.auth.envSupportHint") }}</p>
        </div>

        <!-- Auth Type: login_api -->
        <div v-else-if="profileAuthType === 'login_api'" class="mt-4 space-y-3">
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-12">
            <div class="sm:col-span-6">
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.loginUrlLabel") }}
              </label>
              <input
                v-model="loginApiUrl"
                type="url"
                required
                :placeholder="t('settings.auth.loginUrlPlaceholder')"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div class="sm:col-span-2">
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("explore.auth.loginMethodLabel") }}
              </label>
              <select
                v-model="loginApiMethod"
                data-testid="settings-auth-login-method-select"
                class="input-field !py-1.5 font-mono text-xs"
              >
                <option value="POST">POST</option>
                <option value="PUT">PUT</option>
                <option value="PATCH">PATCH</option>
              </select>
            </div>
            <div class="sm:col-span-4">
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.contentTypeLabel") }}
              </label>
              <div class="flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-xs">
                <button
                  type="button"
                  class="flex-1 rounded-md px-2 py-1 font-medium transition"
                  :class="
                    loginApiContentType === 'json'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="loginApiContentType = 'json'"
                >
                  JSON
                </button>
                <button
                  type="button"
                  class="flex-1 rounded-md px-2 py-1 font-medium transition"
                  :class="
                    loginApiContentType === 'form'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="loginApiContentType = 'form'"
                >
                  Form
                </button>
              </div>
            </div>
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-4">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.usernameFieldLabel") }}
              </label>
              <input
                v-model="loginApiUsernameField"
                type="text"
                placeholder="username"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.usernameLabel") }}
              </label>
              <input
                v-model="loginApiUsername"
                type="text"
                required
                :placeholder="t('settings.auth.usernamePlaceholder')"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.passwordFieldLabel") }}
              </label>
              <input
                v-model="loginApiPasswordField"
                type="text"
                placeholder="password"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.passwordLabel") }}
              </label>
              <div class="relative">
                <input
                  v-model="loginApiPassword"
                  :type="showLoginApiPassword ? 'text' : 'password'"
                  required
                  :placeholder="t('settings.auth.passwordPlaceholder')"
                  class="input-field !py-1.5 pr-8 font-mono text-xs"
                />
                <button
                  type="button"
                  class="absolute right-2 top-1.5 text-slate-400 hover:text-slate-600"
                  @click="showLoginApiPassword = !showLoginApiPassword"
                >
                  <EyeOff v-if="showLoginApiPassword" class="h-3.5 w-3.5" />
                  <Eye v-else class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.tokenJsonpathLabel") }}
              </label>
              <input
                v-model="loginApiTokenJsonpath"
                type="text"
                :placeholder="t('settings.auth.tokenJsonpathPlaceholder')"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.tokenHeaderLabel") }}
              </label>
              <input
                v-model="loginApiTokenHeader"
                type="text"
                placeholder="Authorization"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.tokenPrefixLabel") }}
              </label>
              <input
                v-model="loginApiTokenPrefix"
                type="text"
                placeholder="Bearer "
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
          </div>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("settings.auth.verifyUrlLabel") }}
            </label>
            <input
              v-model="loginApiVerifyUrl"
              type="url"
              :placeholder="t('settings.auth.verifyUrlPlaceholder')"
              class="input-field !py-1.5 font-mono text-xs"
            />
          </div>
          <p class="text-[11px] text-slate-500">{{ t("settings.auth.envSupportHint") }}</p>
        </div>

        <!-- Auth Type: form_login -->
        <div v-else-if="profileAuthType === 'form_login'" class="mt-4 space-y-3">
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.loginUrlLabel") }}
              </label>
              <input
                v-model="formLoginUrl"
                type="url"
                required
                :placeholder="t('settings.auth.formLoginUrlPlaceholder')"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.usernameLabel") }}
              </label>
              <input
                v-model="formLoginUsername"
                type="text"
                required
                :placeholder="t('settings.auth.usernamePlaceholder')"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("settings.auth.passwordLabel") }}
              </label>
              <div class="relative">
                <input
                  v-model="formLoginPassword"
                  :type="showFormLoginPassword ? 'text' : 'password'"
                  required
                  :placeholder="t('settings.auth.passwordPlaceholder')"
                  class="input-field !py-1.5 pr-8 font-mono text-xs"
                />
                <button
                  type="button"
                  class="absolute right-2 top-1.5 text-slate-400 hover:text-slate-600"
                  @click="showFormLoginPassword = !showFormLoginPassword"
                >
                  <EyeOff v-if="showFormLoginPassword" class="h-3.5 w-3.5" />
                  <Eye v-else class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("settings.auth.verifyUrlLabel") }}
            </label>
            <input
              v-model="formLoginVerifyUrl"
              type="url"
              :placeholder="t('settings.auth.verifyUrlPlaceholder')"
              class="input-field !py-1.5 font-mono text-xs"
            />
          </div>

          <div>
            <p class="mb-1.5 text-[11px] font-medium text-slate-600">
              {{ t("settings.auth.selectorsOptional") }}
            </p>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <input
                v-model="formUsernameSelector"
                type="text"
                :placeholder="`${t('settings.auth.usernameSelectorLabel')} (input[name=username])`"
                class="input-field !py-1.5 font-mono text-xs"
              />
              <input
                v-model="formPasswordSelector"
                type="text"
                :placeholder="`${t('settings.auth.passwordSelectorLabel')} (input[type=password])`"
                class="input-field !py-1.5 font-mono text-xs"
              />
              <input
                v-model="formSubmitSelector"
                type="text"
                :placeholder="`${t('settings.auth.submitSelectorLabel')} (button[type=submit])`"
                class="input-field !py-1.5 font-mono text-xs"
              />
            </div>
          </div>
          <p class="text-[11px] text-slate-500">{{ t("settings.auth.envSupportHint") }}</p>
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
                  <span
                    data-testid="profile-auth-type-badge"
                    class="rounded-full px-2 py-0.5 font-mono text-[10px] font-semibold ring-1 ring-inset"
                    :class="profileAuthTypeBadgeClass(profileAuthTypeLabel(profile))"
                  >
                    {{ profileAuthTypeLabel(profile) }}
                  </span>
                </div>
                <p class="mt-0.5 font-mono text-[11px] text-slate-400">ID: {{ profile.id }}</p>
              </div>
              <div class="flex shrink-0 flex-wrap items-center gap-2">
                <button
                  v-if="canTestProfileLogin(profile)"
                  type="button"
                  data-testid="test-login-profile-btn"
                  class="btn-secondary !px-2.5 !py-1.5 !text-xs"
                  :disabled="testingProfileId === profile.id"
                  @click="handleTestProfileLogin(profile)"
                >
                  <Loader2
                    v-if="testingProfileId === profile.id"
                    class="h-3.5 w-3.5 animate-spin text-emerald-600"
                  />
                  <Zap v-else class="h-3.5 w-3.5 text-emerald-600" />
                  <span>
                    {{
                      testingProfileId === profile.id
                        ? t("settings.auth.testingLoginBtn")
                        : t("settings.auth.testLoginBtn")
                    }}
                  </span>
                </button>
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
                v-if="profile.login_config && profile.login_config.login_url"
                class="inline-flex items-center gap-1 rounded-md bg-violet-50 px-2 py-1 font-mono text-[11px] text-violet-800 ring-1 ring-inset ring-violet-600/20"
              >
                <span class="font-semibold">login_url:</span>
                <span>{{ String(profile.login_config.login_url) }}</span>
              </span>
              <span
                v-if="profile.login_config && profile.login_config.username"
                class="inline-flex items-center gap-1 rounded-md bg-slate-100 px-2 py-1 font-mono text-[11px] text-slate-700"
              >
                <span class="font-semibold">username:</span>
                <span>{{ String(profile.login_config.username) }}</span>
              </span>
              <span
                v-if="profile.login_config && profile.login_config.password"
                class="inline-flex items-center gap-1 rounded-md bg-slate-100 px-2 py-1 font-mono text-[11px] text-slate-700"
              >
                <span class="font-semibold">password:</span>
                <span>{{ formatRedactedPassword(profile.login_config.password) }}</span>
              </span>
              <span
                v-if="profile.login_config && profile.login_config.verify_url"
                class="inline-flex items-center gap-1 rounded-md bg-emerald-50 px-2 py-1 font-mono text-[11px] text-emerald-800 ring-1 ring-inset ring-emerald-600/20"
              >
                <span class="font-semibold">verify_url:</span>
                <span>{{ String(profile.login_config.verify_url) }}</span>
              </span>
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
