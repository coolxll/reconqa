<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  Download,
  ExternalLink,
  Eye,
  FileText,
  FolderKanban,
  Globe,
  KeyRound,
  Link2,
  Pencil,
  PlayCircle,
  Plus,
  Search,
  ShieldCheck,
  Tag,
  Trash2,
  TrendingUp,
  Upload,
  X,
} from "lucide-vue-next";
import {
  createAuthProfile,
  createKnowledgeDoc,
  createProject,
  createTarget,
  deleteAuthProfile,
  deleteKnowledgeDoc,
  deleteProject,
  deleteTarget,
  knowledgeDownloadUrl,
  listAuthProfiles,
  listKnowledgeDocs,
  listProjects,
  listTargets,
  updateKnowledgeDoc,
  uploadKnowledgeDoc,
  uploadStorageState,
} from "@/api/projects";
import type {
  AuthProfile,
  KnowledgeDoc,
  KnowledgeDocSourceKind,
  KnowledgeDocType,
  Project,
  RoleTag,
  Target,
} from "@/api/types";
import { useI18n } from "@/i18n";
import StatusBadge from "@/components/layout/StatusBadge.vue";
import EmptyState from "@/components/layout/EmptyState.vue";

interface HeaderRefRow {
  header: string;
  envRef: string;
}

const route = useRoute();
const router = useRouter();
const { locale, t } = useI18n();

const projects = ref<Project[]>([]);
const selectedProjectId = ref<string | null>(null);
const projectSearch = ref("");

const targets = ref<Target[]>([]);
const authProfiles = ref<AuthProfile[]>([]);
const knowledgeDocs = ref<KnowledgeDoc[]>([]);

const loadingProjects = ref(true);
const loadingDetails = ref(false);
const bannerError = ref<string | null>(null);
const bannerSuccess = ref<string | null>(null);

const initialTab =
  route.query.tab === "targets" || route.query.tab === "auth"
    ? (route.query.tab as "targets" | "auth")
    : "knowledge";
const activeTab = ref<"knowledge" | "targets" | "auth">(initialTab);

// Create Project Modal state
const showCreateModal = ref(false);
const creatingProject = ref(false);
const newProjectName = ref("");
const newProjectSlug = ref("");
const newProjectDesc = ref("");

// Delete confirmation state
const confirmDeleteProjectId = ref<string | null>(null);

// Add Target form state
const addingTarget = ref(false);
const targetRootDomain = ref("");
const targetBaseUrl = ref("");

// Add Auth Profile form state
const addingProfile = ref(false);
const profileName = ref("");
const profileRole = ref<RoleTag>("account_a");
const headerRefRows = ref<HeaderRefRow[]>([
  { header: "Authorization", envRef: "ENV:RECONQA_ACCOUNT_A_TOKEN" },
]);
const uploadingProfileId = ref<string | null>(null);

// Knowledge Base state
const kbCategoryFilter = ref<"all" | KnowledgeDocType>("all");
const kbSearch = ref("");

// Create/Edit Knowledge Doc Modal state
const showKbDocModal = ref(false);
const editingDocId = ref<string | null>(null);
const savingKbDoc = ref(false);
const kbFormTitle = ref("");
const kbFormType = ref<KnowledgeDocType>("prd");
const kbFormSource = ref<KnowledgeDocSourceKind>("markdown");
const kbFormExternalUrl = ref("");
const kbFormContent = ref("");
const kbFormTags = ref("");
const kbFormEndpoints = ref("");

// Upload Knowledge File Modal state
const showKbUploadModal = ref(false);
const uploadingKbFile = ref(false);
const kbUploadFile = ref<File | null>(null);
const kbUploadTitle = ref("");
const kbUploadType = ref<KnowledgeDocType>("prd");
const kbUploadTags = ref("");
const kbUploadEndpoints = ref("");

// Preview Knowledge Doc Modal state
const previewDoc = ref<KnowledgeDoc | null>(null);

const kbDocTypes: KnowledgeDocType[] = [
  "prd",
  "api_spec",
  "business_rule",
  "test_notes",
  "architecture",
  "other",
];

const filteredProjects = computed(() => {
  const q = projectSearch.value.trim().toLowerCase();
  if (!q) return projects.value;
  return projects.value.filter(
    (p) =>
      p.name.toLowerCase().includes(q) ||
      p.slug.toLowerCase().includes(q) ||
      p.description.toLowerCase().includes(q)
  );
});

const selectedProject = computed(
  () => projects.value.find((p) => p.id === selectedProjectId.value) ?? null
);

const hasAccountA = computed(() =>
  authProfiles.value.some((p) => p.role_tag === "account_a")
);
const hasAccountB = computed(() =>
  authProfiles.value.some((p) => p.role_tag === "account_b")
);

const filteredKnowledgeDocs = computed(() => {
  const typeFilter = kbCategoryFilter.value;
  const q = kbSearch.value.trim().toLowerCase();
  return knowledgeDocs.value.filter((doc) => {
    if (typeFilter !== "all" && doc.doc_type !== typeFilter) {
      return false;
    }
    if (!q) return true;
    const haystack = [
      doc.title,
      doc.content,
      doc.external_url ?? "",
      doc.original_filename ?? "",
      ...(doc.tags ?? []),
      ...(doc.related_endpoints ?? []),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(q);
  });
});

function notifySuccess(msg: string): void {
  bannerError.value = null;
  bannerSuccess.value = msg;
  window.setTimeout(() => {
    if (bannerSuccess.value === msg) {
      bannerSuccess.value = null;
    }
  }, 4000);
}

function notifyError(err: unknown, fallback: string): void {
  bannerSuccess.value = null;
  bannerError.value = err instanceof Error ? err.message : fallback;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(locale.value === "zh" ? "zh-CN" : "en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function storageStateBasename(fullPath: string | null): string {
  if (!fullPath) return "";
  const parts = fullPath.split(/[\\/]/);
  return parts[parts.length - 1] || fullPath;
}

function parseCsvList(raw: string): string[] {
  return raw
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function docTypeBadgeClass(docType: string): string {
  switch (docType) {
    case "prd":
      return "bg-indigo-50 text-indigo-700 ring-indigo-600/20";
    case "api_spec":
      return "bg-sky-50 text-sky-700 ring-sky-600/20";
    case "business_rule":
      return "bg-amber-50 text-amber-800 ring-amber-600/20";
    case "test_notes":
      return "bg-emerald-50 text-emerald-700 ring-emerald-600/20";
    case "architecture":
      return "bg-violet-50 text-violet-700 ring-violet-600/20";
    default:
      return "bg-slate-100 text-slate-700 ring-slate-500/20";
  }
}

function docTypeLabel(docType: string): string {
  const key = `projects.kb.type.${docType}`;
  const translated = t(key);
  return translated === key ? docType : translated;
}

function docSourceLabel(sourceKind: string): string {
  const key = `projects.kb.source.${sourceKind}`;
  const translated = t(key);
  return translated === key ? sourceKind : translated;
}

async function fetchProjects(): Promise<void> {
  loadingProjects.value = true;
  bannerError.value = null;
  try {
    const list = await listProjects();
    projects.value = list;
    const queryProj = typeof route.query.projectId === "string" ? route.query.projectId : null;
    if (queryProj && list.some((p) => p.id === queryProj)) {
      selectedProjectId.value = queryProj;
    } else if (
      selectedProjectId.value &&
      list.some((p) => p.id === selectedProjectId.value)
    ) {
      // keep current selection
    } else {
      selectedProjectId.value = list[0]?.id ?? null;
    }
  } catch (err) {
    notifyError(err, t("projects.err.loadProjects"));
  } finally {
    loadingProjects.value = false;
  }
}

async function fetchProjectDetails(projectId: string): Promise<void> {
  loadingDetails.value = true;
  try {
    const [tList, aList, kList] = await Promise.all([
      listTargets(projectId),
      listAuthProfiles(projectId),
      listKnowledgeDocs(projectId).catch(() => [] as KnowledgeDoc[]),
    ]);
    targets.value = tList;
    authProfiles.value = aList;
    knowledgeDocs.value = kList;
  } catch (err) {
    notifyError(err, t("projects.err.loadDetails"));
  } finally {
    loadingDetails.value = false;
  }
}

watch(selectedProjectId, (id) => {
  previewDoc.value = null;
  if (id) {
    void fetchProjectDetails(id);
  } else {
    targets.value = [];
    authProfiles.value = [];
    knowledgeDocs.value = [];
  }
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

async function handleCreateProject(): Promise<void> {
  if (!newProjectName.value.trim()) return;
  creatingProject.value = true;
  try {
    const created = await createProject({
      name: newProjectName.value.trim(),
      slug: newProjectSlug.value.trim() || null,
      description: newProjectDesc.value.trim(),
    });
    projects.value = [created, ...projects.value];
    selectedProjectId.value = created.id;
    showCreateModal.value = false;
    newProjectName.value = "";
    newProjectSlug.value = "";
    newProjectDesc.value = "";
    notifySuccess(t("projects.toast.projectCreated", { name: created.name }));
  } catch (err) {
    notifyError(err, t("projects.err.createProject"));
  } finally {
    creatingProject.value = false;
  }
}

async function handleDeleteProject(projectId: string): Promise<void> {
  try {
    await deleteProject(projectId);
    projects.value = projects.value.filter((p) => p.id !== projectId);
    confirmDeleteProjectId.value = null;
    if (selectedProjectId.value === projectId) {
      selectedProjectId.value = projects.value[0]?.id ?? null;
    }
    notifySuccess(t("projects.toast.projectDeleted"));
  } catch (err) {
    notifyError(err, t("projects.err.deleteProject"));
  }
}

async function handleAddTarget(): Promise<void> {
  if (!selectedProjectId.value || !targetRootDomain.value.trim() || !targetBaseUrl.value.trim()) {
    return;
  }
  addingTarget.value = true;
  try {
    const created = await createTarget(selectedProjectId.value, {
      root_domain: targetRootDomain.value.trim(),
      base_url: targetBaseUrl.value.trim(),
      scope_rules: {},
    });
    targets.value = [...targets.value, created];
    targetRootDomain.value = "";
    targetBaseUrl.value = "";
    notifySuccess(t("projects.toast.targetAdded", { domain: created.root_domain }));
  } catch (err) {
    notifyError(err, t("projects.err.createTarget"));
  } finally {
    addingTarget.value = false;
  }
}

async function handleDeleteTarget(targetId: string): Promise<void> {
  if (!selectedProjectId.value) return;
  try {
    await deleteTarget(selectedProjectId.value, targetId);
    targets.value = targets.value.filter((item) => item.id !== targetId);
    notifySuccess(t("projects.toast.targetDeleted"));
  } catch (err) {
    notifyError(err, t("projects.err.deleteTarget"));
  }
}

function addHeaderRefRow(): void {
  headerRefRows.value.push({ header: "", envRef: "" });
}

function removeHeaderRefRow(idx: number): void {
  headerRefRows.value.splice(idx, 1);
}

async function handleAddAuthProfile(): Promise<void> {
  if (!selectedProjectId.value || !profileName.value.trim()) return;
  addingProfile.value = true;
  try {
    const extraHeadersRef: Record<string, string> = {};
    for (const row of headerRefRows.value) {
      const k = row.header.trim();
      const v = row.envRef.trim();
      if (k && v) {
        extraHeadersRef[k] = v;
      }
    }
    const created = await createAuthProfile(selectedProjectId.value, {
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
  if (!selectedProjectId.value) return;
  try {
    await deleteAuthProfile(selectedProjectId.value, profileId);
    authProfiles.value = authProfiles.value.filter((p) => p.id !== profileId);
    notifySuccess(t("projects.toast.profileDeleted"));
  } catch (err) {
    notifyError(err, t("projects.err.deleteProfile"));
  }
}

async function handleUploadStorageState(profile: AuthProfile, event: Event): Promise<void> {
  if (!selectedProjectId.value) return;
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;

  uploadingProfileId.value = profile.id;
  try {
    const updated = await uploadStorageState(selectedProjectId.value, profile.id, file);
    authProfiles.value = authProfiles.value.map((p) => (p.id === updated.id ? updated : p));
    notifySuccess(t("projects.toast.stateUploaded", { name: profile.name }));
  } catch (err) {
    notifyError(err, t("projects.err.uploadState"));
  } finally {
    uploadingProfileId.value = null;
    input.value = "";
  }
}

// --- Knowledge Base handlers ---

function openCreateKbModal(): void {
  editingDocId.value = null;
  kbFormTitle.value = "";
  kbFormType.value = "prd";
  kbFormSource.value = "markdown";
  kbFormExternalUrl.value = "";
  kbFormContent.value = "";
  kbFormTags.value = "";
  kbFormEndpoints.value = "";
  showKbDocModal.value = true;
}

function openEditKbModal(doc: KnowledgeDoc): void {
  previewDoc.value = null;
  editingDocId.value = doc.id;
  kbFormTitle.value = doc.title;
  kbFormType.value = (
    kbDocTypes.includes(doc.doc_type as KnowledgeDocType) ? doc.doc_type : "other"
  ) as KnowledgeDocType;
  kbFormSource.value = (
    doc.source_kind === "link" || doc.source_kind === "file" ? doc.source_kind : "markdown"
  ) as KnowledgeDocSourceKind;
  kbFormExternalUrl.value = doc.external_url ?? "";
  kbFormContent.value = doc.content ?? "";
  kbFormTags.value = (doc.tags ?? []).join(", ");
  kbFormEndpoints.value = (doc.related_endpoints ?? []).join(", ");
  showKbDocModal.value = true;
}

async function handleSaveKbDoc(): Promise<void> {
  if (!selectedProjectId.value || !kbFormTitle.value.trim()) return;
  savingKbDoc.value = true;
  try {
    const tags = parseCsvList(kbFormTags.value);
    const related_endpoints = parseCsvList(kbFormEndpoints.value);
    if (editingDocId.value) {
      const updated = await updateKnowledgeDoc(selectedProjectId.value, editingDocId.value, {
        title: kbFormTitle.value.trim(),
        doc_type: kbFormType.value,
        content: kbFormContent.value,
        external_url: kbFormExternalUrl.value.trim() || null,
        tags,
        related_endpoints,
      });
      knowledgeDocs.value = knowledgeDocs.value.map((d) => (d.id === updated.id ? updated : d));
      showKbDocModal.value = false;
      notifySuccess(t("projects.kb.toast.updated", { title: updated.title }));
    } else {
      const created = await createKnowledgeDoc(selectedProjectId.value, {
        title: kbFormTitle.value.trim(),
        doc_type: kbFormType.value,
        source_kind: kbFormSource.value,
        content: kbFormContent.value,
        external_url: kbFormSource.value === "link" ? kbFormExternalUrl.value.trim() || null : null,
        tags,
        related_endpoints,
      });
      knowledgeDocs.value = [created, ...knowledgeDocs.value];
      showKbDocModal.value = false;
      notifySuccess(t("projects.kb.toast.created", { title: created.title }));
    }
  } catch (err) {
    notifyError(err, t("projects.kb.err.save"));
  } finally {
    savingKbDoc.value = false;
  }
}

function openUploadKbModal(): void {
  kbUploadFile.value = null;
  kbUploadTitle.value = "";
  kbUploadType.value = "prd";
  kbUploadTags.value = "";
  kbUploadEndpoints.value = "";
  showKbUploadModal.value = true;
}

function onKbFileSelected(event: Event): void {
  const input = event.target as HTMLInputElement;
  kbUploadFile.value = input.files?.[0] ?? null;
}

async function handleUploadKbFile(): Promise<void> {
  if (!selectedProjectId.value || !kbUploadFile.value) return;
  uploadingKbFile.value = true;
  try {
    const created = await uploadKnowledgeDoc(selectedProjectId.value, kbUploadFile.value, {
      title: kbUploadTitle.value.trim() || undefined,
      doc_type: kbUploadType.value,
      tags: parseCsvList(kbUploadTags.value),
      related_endpoints: parseCsvList(kbUploadEndpoints.value),
    });
    knowledgeDocs.value = [created, ...knowledgeDocs.value];
    showKbUploadModal.value = false;
    notifySuccess(t("projects.kb.toast.uploaded", { title: created.title }));
  } catch (err) {
    notifyError(err, t("projects.kb.err.upload"));
  } finally {
    uploadingKbFile.value = false;
  }
}

async function handleDeleteKbDoc(docId: string): Promise<void> {
  if (!selectedProjectId.value) return;
  try {
    await deleteKnowledgeDoc(selectedProjectId.value, docId);
    knowledgeDocs.value = knowledgeDocs.value.filter((d) => d.id !== docId);
    if (previewDoc.value?.id === docId) {
      previewDoc.value = null;
    }
    notifySuccess(t("projects.kb.toast.deleted"));
  } catch (err) {
    notifyError(err, t("projects.kb.err.delete"));
  }
}

function launchRunForTarget(target: Target): void {
  void router.push({
    name: "runs",
    query: {
      new: "1",
      projectId: target.project_id,
      targetId: target.id,
      targetUrl: target.base_url,
    },
  });
}

onMounted(() => {
  void fetchProjects();
});
</script>

<template>
  <div class="space-y-4">
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

    <!-- Master-Detail Split Grid -->
    <div class="grid grid-cols-1 gap-6 lg:grid-cols-12">
      <!-- Left: Project List -->
      <aside class="card flex flex-col overflow-hidden lg:col-span-4">
        <div class="card-header">
          <div class="flex items-center gap-2">
            <FolderKanban class="h-4 w-4 text-indigo-600" />
            <h2 class="text-sm font-semibold text-slate-900">
              {{ t("projects.listTitle") }}
              <span class="ml-1 rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
                {{ projects.length }}
              </span>
            </h2>
          </div>
          <button
            type="button"
            class="btn-primary !px-2.5 !py-1.5 !text-xs"
            @click="showCreateModal = true"
          >
            <Plus class="h-3.5 w-3.5" />
            <span>{{ t("projects.newBtn") }}</span>
          </button>
        </div>

        <div class="border-b border-slate-100 p-3">
          <div class="relative">
            <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
            <input
              v-model="projectSearch"
              type="text"
              :placeholder="t('projects.searchPlaceholder')"
              class="input-field !py-1.5 !pl-9"
            />
          </div>
        </div>

        <div v-if="loadingProjects" class="p-6 text-center text-xs text-slate-500">
          {{ t("projects.loading") }}
        </div>

        <div v-else-if="filteredProjects.length === 0" class="p-5">
          <EmptyState
            :title="t('projects.emptyTitle')"
            :description="t('projects.emptyDesc')"
          >
            <template #action>
              <button type="button" class="btn-primary !text-xs" @click="showCreateModal = true">
                <Plus class="h-3.5 w-3.5" />
                <span>{{ t("projects.createBtn") }}</span>
              </button>
            </template>
          </EmptyState>
        </div>

        <ul v-else class="divide-y divide-slate-100 overflow-y-auto">
          <li
            v-for="proj in filteredProjects"
            :key="proj.id"
            class="group cursor-pointer border-l-2 px-4 py-3.5 transition"
            :class="
              proj.id === selectedProjectId
                ? 'border-l-indigo-600 bg-indigo-50/70'
                : 'border-l-transparent hover:bg-slate-50'
            "
            @click="selectedProjectId = proj.id"
          >
            <div class="flex items-start justify-between gap-2">
              <div class="min-w-0">
                <p
                  class="truncate text-sm font-semibold"
                  :class="proj.id === selectedProjectId ? 'text-indigo-900' : 'text-slate-900'"
                >
                  {{ proj.name }}
                </p>
                <p class="mt-0.5 truncate font-mono text-[11px] text-slate-500">
                  {{ proj.slug }}
                </p>
              </div>
              <div class="flex shrink-0 items-center gap-1" @click.stop>
                <template v-if="confirmDeleteProjectId === proj.id">
                  <button
                    type="button"
                    class="rounded bg-rose-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-rose-700"
                    @click="handleDeleteProject(proj.id)"
                  >
                    {{ t("common.confirm") }}
                  </button>
                  <button
                    type="button"
                    class="rounded bg-slate-200 px-1.5 py-0.5 text-[11px] text-slate-700 hover:bg-slate-300"
                    @click="confirmDeleteProjectId = null"
                  >
                    {{ t("common.cancel") }}
                  </button>
                </template>
                <button
                  v-else
                  type="button"
                  :title="t('projects.deleteTitle')"
                  class="rounded p-1 text-slate-400 opacity-70 transition hover:bg-rose-50 hover:text-rose-600 group-hover:opacity-100"
                  @click="confirmDeleteProjectId = proj.id"
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            <p v-if="proj.description" class="mt-1 line-clamp-2 text-xs text-slate-500">
              {{ proj.description }}
            </p>
            <p class="mt-1.5 text-[11px] text-slate-400">
              {{ t("projects.createdAt", { date: formatDate(proj.created_at) }) }}
            </p>
          </li>
        </ul>
      </aside>

      <!-- Right: Selected Project Workspace -->
      <section class="lg:col-span-8">
        <div v-if="!selectedProject" class="card p-8">
          <EmptyState
            :title="t('projects.selectTitle')"
            :description="t('projects.selectDesc')"
          >
            <template #action>
              <button type="button" class="btn-primary" @click="showCreateModal = true">
                <Plus class="h-4 w-4" />
                <span>{{ t("projects.createBtn") }}</span>
              </button>
            </template>
          </EmptyState>
        </div>

        <div v-else class="space-y-5">
          <!-- Selected Project Header -->
          <div class="card p-5">
            <div class="flex flex-col justify-between gap-4 sm:flex-row sm:items-start">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <h2 class="text-lg font-bold text-slate-900">{{ selectedProject.name }}</h2>
                  <span
                    class="rounded-md bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-600"
                  >
                    {{ selectedProject.slug }}
                  </span>
                </div>
                <p class="mt-1 text-sm text-slate-600">
                  {{ selectedProject.description || t("projects.noDesc") }}
                </p>
                <p class="mt-1.5 font-mono text-[11px] text-slate-400">
                  {{ t("projects.projectId", { id: selectedProject.id }) }}
                </p>
              </div>

              <div class="flex shrink-0 flex-wrap items-center gap-2">
                <router-link
                  :to="{ name: 'trends', query: { projectId: selectedProject.id } }"
                  class="btn-secondary !text-xs"
                >
                  <TrendingUp class="h-3.5 w-3.5" />
                  <span>{{ t("projects.trendsBtn") }}</span>
                </router-link>
                <router-link
                  :to="{ name: 'runs', query: { new: '1', projectId: selectedProject.id } }"
                  class="btn-primary !text-xs"
                >
                  <PlayCircle class="h-3.5 w-3.5" />
                  <span>{{ t("projects.launchRunBtn") }}</span>
                </router-link>
              </div>
            </div>

            <!-- Tabs: Knowledge Base (default) | Targets | Credentials -->
            <div class="mt-5 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-4">
              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-xs font-semibold transition"
                :class="
                  activeTab === 'knowledge'
                    ? 'bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-600/20'
                    : 'text-slate-600 hover:bg-slate-100'
                "
                data-testid="project-tab-knowledge"
                @click="activeTab = 'knowledge'"
              >
                <BookOpen class="h-3.5 w-3.5" />
                <span>{{ t("projects.tab.knowledge") }}</span>
                <span class="rounded-full bg-white px-1.5 py-0.5 text-[10px] shadow-sm">
                  {{ knowledgeDocs.length }}
                </span>
              </button>

              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-xs font-semibold transition"
                :class="
                  activeTab === 'targets'
                    ? 'bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-600/20'
                    : 'text-slate-600 hover:bg-slate-100'
                "
                data-testid="project-tab-targets"
                @click="activeTab = 'targets'"
              >
                <Globe class="h-3.5 w-3.5" />
                <span>{{ t("projects.tab.targets") }}</span>
                <span class="rounded-full bg-white px-1.5 py-0.5 text-[10px] shadow-sm">
                  {{ targets.length }}
                </span>
              </button>

              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-xs font-semibold transition"
                :class="
                  activeTab === 'auth'
                    ? 'bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-600/20'
                    : 'text-slate-600 hover:bg-slate-100'
                "
                data-testid="project-tab-auth"
                @click="activeTab = 'auth'"
              >
                <KeyRound class="h-3.5 w-3.5" />
                <span>{{ t("projects.tab.auth") }}</span>
                <span class="rounded-full bg-white px-1.5 py-0.5 text-[10px] shadow-sm">
                  {{ authProfiles.length }}
                </span>
              </button>
            </div>
          </div>

          <!-- TAB 1: Targets -->
          <div v-if="activeTab === 'targets'" class="space-y-4">
            <!-- Add Target Form -->
            <form class="card p-5" @submit.prevent="handleAddTarget">
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("projects.target.formTitle") }}
              </h3>
              <p class="mt-0.5 text-xs text-slate-500">
                {{ t("projects.target.formDesc") }}
              </p>
              <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
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
                  <span>
                    {{ addingTarget ? t("projects.target.adding") : t("projects.target.addBtn") }}
                  </span>
                </button>
              </div>
            </form>

            <!-- Targets List -->
            <div class="card overflow-hidden">
              <div class="card-header">
                <h3 class="text-sm font-semibold text-slate-900">
                  {{ t("projects.target.listTitle") }}
                </h3>
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
                    <div class="flex items-center gap-2">
                      <Globe class="h-4 w-4 shrink-0 text-indigo-500" />
                      <span class="truncate font-mono text-sm font-semibold text-slate-900">
                        {{ target.root_domain }}
                      </span>
                    </div>
                    <p class="mt-0.5 truncate font-mono text-xs text-slate-500">
                      {{ target.base_url }}
                    </p>
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

          <!-- TAB 2: Dual-Account Auth Profiles -->
          <div v-else-if="activeTab === 'auth'" class="space-y-4">
            <!-- Security Note + Dual-Account Readiness -->
            <div
              class="rounded-xl border border-indigo-200/80 bg-indigo-50/50 p-4 text-xs text-indigo-950"
            >
              <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
                <div class="flex items-start gap-2.5">
                  <ShieldCheck class="mt-0.5 h-4 w-4 shrink-0 text-indigo-600" />
                  <div class="space-y-1">
                    <p class="font-semibold">
                      {{ t("projects.auth.hygieneTitle") }}
                    </p>
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

            <!-- Add Auth Profile Form -->
            <form class="card p-5" @submit.prevent="handleAddAuthProfile">
              <h3 class="text-sm font-semibold text-slate-900">
                {{ t("projects.auth.formTitle") }}
              </h3>
              <p class="mt-0.5 text-xs text-slate-500">
                {{ t("projects.auth.formDesc") }}
              </p>

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
                  <select v-model="profileRole" class="input-field text-xs">
                    <option value="account_a">{{ t("projects.auth.roleA") }}</option>
                    <option value="account_b">{{ t("projects.auth.roleB") }}</option>
                    <option value="guest">{{ t("projects.auth.roleGuest") }}</option>
                  </select>
                </div>
              </div>

              <!-- extra_headers_ref mappings -->
              <div class="mt-4">
                <div class="flex items-center justify-between">
                  <label class="text-xs font-medium text-slate-700">
                    {{ t("projects.auth.headersLabel") }}
                  </label>
                  <button
                    type="button"
                    class="text-xs font-medium text-indigo-600 hover:text-indigo-800"
                    @click="addHeaderRefRow"
                  >
                    {{ t("projects.auth.addHeaderBtn") }}
                  </button>
                </div>

                <div class="mt-2 space-y-2">
                  <div
                    v-for="(row, idx) in headerRefRows"
                    :key="idx"
                    class="flex items-center gap-2"
                  >
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
                      placeholder="ENV:RECONQA_ACCOUNT_A_TOKEN"
                      class="input-field !py-1.5 font-mono text-xs"
                    />
                    <button
                      type="button"
                      class="rounded p-1.5 text-slate-400 hover:bg-slate-100 hover:text-rose-600"
                      :title="t('projects.auth.removeHeaderTitle')"
                      @click="removeHeaderRefRow(idx)"
                    >
                      <X class="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              </div>

              <div class="mt-4 flex justify-end">
                <button type="submit" class="btn-primary !text-xs" :disabled="addingProfile">
                  <Plus class="h-3.5 w-3.5" />
                  <span>
                    {{
                      addingProfile ? t("projects.auth.creating") : t("projects.auth.createBtn")
                    }}
                  </span>
                </button>
              </div>
            </form>

            <!-- Auth Profiles List -->
            <div class="card overflow-hidden">
              <div class="card-header">
                <h3 class="text-sm font-semibold text-slate-900">
                  {{ t("projects.auth.listTitle") }}
                </h3>
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
                <div
                  v-for="profile in authProfiles"
                  :key="profile.id"
                  class="space-y-3 px-5 py-4"
                >
                  <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
                    <div class="min-w-0">
                      <div class="flex flex-wrap items-center gap-2">
                        <span class="text-sm font-semibold text-slate-900">{{ profile.name }}</span>
                        <StatusBadge :status="profile.role_tag" />
                      </div>
                      <p class="mt-0.5 font-mono text-[11px] text-slate-400">
                        ID: {{ profile.id }}
                      </p>
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

                  <!-- Storage State + Header Refs summary -->
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
                      <span>{{ envVal }}</span>
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- TAB 3: Project Knowledge Base -->
          <div v-else class="space-y-4">
            <!-- Knowledge Base Info Banner & Action Bar -->
            <div
              class="rounded-xl border border-indigo-200/80 bg-indigo-50/50 p-4 text-xs text-indigo-950"
            >
              <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
                <div class="flex items-start gap-2.5">
                  <BookOpen class="mt-0.5 h-4 w-4 shrink-0 text-indigo-600" />
                  <div class="space-y-1">
                    <p class="font-semibold">{{ t("projects.kb.bannerTitle") }}</p>
                    <p class="leading-relaxed text-indigo-900/80">
                      {{ t("projects.kb.bannerDesc") }}
                    </p>
                  </div>
                </div>

                <div class="flex shrink-0 flex-wrap items-center gap-2">
                  <button
                    type="button"
                    class="btn-secondary !px-3 !py-1.5 !text-xs"
                    data-testid="kb-upload-btn"
                    @click="openUploadKbModal"
                  >
                    <Upload class="h-3.5 w-3.5 text-indigo-600" />
                    <span>{{ t("projects.kb.uploadBtn") }}</span>
                  </button>
                  <button
                    type="button"
                    class="btn-primary !px-3 !py-1.5 !text-xs"
                    data-testid="kb-new-doc-btn"
                    @click="openCreateKbModal"
                  >
                    <Plus class="h-3.5 w-3.5" />
                    <span>{{ t("projects.kb.newDocBtn") }}</span>
                  </button>
                </div>
              </div>
            </div>

            <!-- Filter Pills + Search -->
            <div class="card p-4 space-y-3">
              <div class="flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  class="rounded-full px-3 py-1 text-xs font-medium transition"
                  :class="
                    kbCategoryFilter === 'all'
                      ? 'bg-indigo-600 text-white shadow-sm'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                  "
                  @click="kbCategoryFilter = 'all'"
                >
                  {{ t("projects.kb.filter.all") }} ({{ knowledgeDocs.length }})
                </button>
                <button
                  v-for="dt in kbDocTypes"
                  :key="dt"
                  type="button"
                  class="rounded-full px-3 py-1 text-xs font-medium transition"
                  :class="
                    kbCategoryFilter === dt
                      ? 'bg-indigo-600 text-white shadow-sm'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                  "
                  @click="kbCategoryFilter = dt"
                >
                  {{ docTypeLabel(dt) }}
                </button>
              </div>

              <div class="relative">
                <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
                <input
                  v-model="kbSearch"
                  type="text"
                  :placeholder="t('projects.kb.searchPlaceholder')"
                  class="input-field !py-1.5 !pl-9 text-xs"
                  data-testid="kb-search-input"
                />
              </div>
            </div>

            <!-- Documents List -->
            <div class="card overflow-hidden">
              <div v-if="loadingDetails" class="p-6 text-center text-xs text-slate-500">
                {{ t("projects.kb.loading") }}
              </div>

              <div v-else-if="filteredKnowledgeDocs.length === 0" class="p-6">
                <EmptyState
                  :title="t('projects.kb.emptyTitle')"
                  :description="t('projects.kb.emptyDesc')"
                >
                  <template #icon>
                    <BookOpen class="h-5 w-5" />
                  </template>
                  <template #action>
                    <button
                      type="button"
                      class="btn-secondary !text-xs"
                      @click="openUploadKbModal"
                    >
                      <Upload class="h-3.5 w-3.5" />
                      <span>{{ t("projects.kb.uploadBtn") }}</span>
                    </button>
                    <button
                      type="button"
                      class="btn-primary !text-xs"
                      @click="openCreateKbModal"
                    >
                      <Plus class="h-3.5 w-3.5" />
                      <span>{{ t("projects.kb.newDocBtn") }}</span>
                    </button>
                  </template>
                </EmptyState>
              </div>

              <div v-else class="divide-y divide-slate-100">
                <div
                  v-for="doc in filteredKnowledgeDocs"
                  :key="doc.id"
                  class="space-y-2.5 px-5 py-4 transition hover:bg-slate-50/70"
                  data-testid="kb-doc-item"
                >
                  <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
                    <div class="min-w-0 space-y-1">
                      <div class="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          class="text-left text-sm font-semibold text-slate-900 hover:text-indigo-700 hover:underline"
                          @click="previewDoc = doc"
                        >
                          {{ doc.title }}
                        </button>
                        <span
                          class="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset"
                          :class="docTypeBadgeClass(doc.doc_type)"
                        >
                          {{ docTypeLabel(doc.doc_type) }}
                        </span>
                        <span
                          class="inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600"
                        >
                          <Link2 v-if="doc.source_kind === 'link'" class="h-2.5 w-2.5" />
                          <FileText v-else class="h-2.5 w-2.5" />
                          <span>{{ docSourceLabel(doc.source_kind) }}</span>
                        </span>
                      </div>

                      <p
                        v-if="doc.content"
                        class="line-clamp-2 text-xs leading-relaxed text-slate-600"
                      >
                        {{ doc.content }}
                      </p>
                      <a
                        v-else-if="doc.external_url"
                        :href="doc.external_url"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="inline-flex items-center gap-1 font-mono text-xs text-indigo-600 hover:underline"
                      >
                        <span>{{ doc.external_url }}</span>
                        <ExternalLink class="h-3 w-3" />
                      </a>

                      <p class="text-[11px] text-slate-400">
                        {{ t("projects.kb.updatedAt", { date: formatDate(doc.updated_at) }) }}
                        <span v-if="doc.size_bytes > 0"> · {{ formatBytes(doc.size_bytes) }}</span>
                        <span v-if="doc.original_filename" class="font-mono">
                          · {{ doc.original_filename }}
                        </span>
                      </p>
                    </div>

                    <div class="flex shrink-0 flex-wrap items-center gap-1.5">
                      <button
                        type="button"
                        class="btn-secondary !px-2 !py-1 !text-xs"
                        @click="previewDoc = doc"
                      >
                        <Eye class="h-3.5 w-3.5" />
                        <span>{{ t("projects.kb.viewBtn") }}</span>
                      </button>
                      <button
                        type="button"
                        class="btn-secondary !px-2 !py-1 !text-xs"
                        @click="openEditKbModal(doc)"
                      >
                        <Pencil class="h-3.5 w-3.5" />
                        <span>{{ t("projects.kb.editBtn") }}</span>
                      </button>
                      <a
                        v-if="doc.source_kind !== 'link' && selectedProject"
                        :href="knowledgeDownloadUrl(selectedProject.id, doc.id)"
                        download
                        class="btn-secondary !px-2 !py-1 !text-xs"
                      >
                        <Download class="h-3.5 w-3.5" />
                        <span>{{ t("projects.kb.downloadBtn") }}</span>
                      </a>
                      <a
                        v-else-if="doc.external_url"
                        :href="doc.external_url"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="btn-secondary !px-2 !py-1 !text-xs"
                      >
                        <ExternalLink class="h-3.5 w-3.5" />
                        <span>{{ t("projects.kb.openLinkBtn") }}</span>
                      </a>
                      <button
                        type="button"
                        class="btn-danger !px-2 !py-1 !text-xs"
                        :title="t('projects.kb.deleteTitle')"
                        @click="handleDeleteKbDoc(doc.id)"
                      >
                        <Trash2 class="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>

                  <!-- Tags & Related Endpoints -->
                  <div
                    v-if="doc.tags.length > 0 || doc.related_endpoints.length > 0"
                    class="flex flex-wrap items-center gap-1.5 pt-0.5"
                  >
                    <span
                      v-for="tag in doc.tags"
                      :key="`tag-${tag}`"
                      class="inline-flex items-center gap-1 rounded-md bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600"
                    >
                      <Tag class="h-2.5 w-2.5 text-slate-400" />
                      <span>{{ tag }}</span>
                    </span>
                    <span
                      v-for="ep in doc.related_endpoints"
                      :key="`ep-${ep}`"
                      class="inline-flex items-center rounded-md bg-indigo-50 px-2 py-0.5 font-mono text-[11px] text-indigo-700 ring-1 ring-inset ring-indigo-600/15"
                    >
                      {{ ep }}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- Create Project Modal -->
    <div
      v-if="showCreateModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
      @click.self="showCreateModal = false"
    >
      <div class="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 shadow-xl">
        <div class="flex items-center justify-between">
          <h3 class="text-base font-semibold text-slate-900">{{ t("projects.modal.title") }}</h3>
          <button
            type="button"
            class="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            @click="showCreateModal = false"
          >
            <X class="h-4 w-4" />
          </button>
        </div>
        <p class="mt-1 text-xs text-slate-500">
          {{ t("projects.modal.desc") }}
        </p>

        <form class="mt-4 space-y-4" @submit.prevent="handleCreateProject">
          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.modal.nameLabel") }}
            </label>
            <input
              v-model="newProjectName"
              type="text"
              required
              :placeholder="t('projects.modal.namePlaceholder')"
              class="input-field"
            />
          </div>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.modal.slugLabel") }}
              <span class="font-normal text-slate-400">{{ t("projects.modal.slugHint") }}</span>
            </label>
            <input
              v-model="newProjectSlug"
              type="text"
              placeholder="example-storefront"
              class="input-field font-mono text-xs"
            />
          </div>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.modal.descLabel") }}
            </label>
            <textarea
              v-model="newProjectDesc"
              rows="3"
              :placeholder="t('projects.modal.descPlaceholder')"
              class="input-field"
            />
          </div>

          <div class="flex items-center justify-end gap-2 pt-2">
            <button type="button" class="btn-secondary" @click="showCreateModal = false">
              {{ t("common.cancel") }}
            </button>
            <button type="submit" class="btn-primary" :disabled="creatingProject">
              {{ creatingProject ? t("projects.modal.creating") : t("projects.modal.submit") }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- Create / Edit Knowledge Doc Modal -->
    <div
      v-if="showKbDocModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
      data-testid="kb-doc-modal"
      @click.self="showKbDocModal = false"
    >
      <div class="w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
        <div class="flex items-center justify-between border-b border-slate-100 px-6 py-4">
          <h3 class="text-base font-semibold text-slate-900">
            {{
              editingDocId ? t("projects.kb.modal.editTitle") : t("projects.kb.modal.createTitle")
            }}
          </h3>
          <button
            type="button"
            class="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            @click="showKbDocModal = false"
          >
            <X class="h-4 w-4" />
          </button>
        </div>

        <form class="max-h-[80vh] space-y-4 overflow-y-auto px-6 py-5" @submit.prevent="handleSaveKbDoc">
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div class="sm:col-span-2">
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.kb.modal.titleLabel") }}
              </label>
              <input
                v-model="kbFormTitle"
                type="text"
                required
                :placeholder="t('projects.kb.modal.titlePlaceholder')"
                class="input-field"
                data-testid="kb-form-title"
              />
            </div>

            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.kb.modal.typeLabel") }}
              </label>
              <select v-model="kbFormType" class="input-field text-xs" data-testid="kb-form-type">
                <option v-for="dt in kbDocTypes" :key="dt" :value="dt">
                  {{ docTypeLabel(dt) }}
                </option>
              </select>
            </div>

            <div v-if="!editingDocId">
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.kb.modal.sourceLabel") }}
              </label>
              <div class="flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-xs">
                <button
                  type="button"
                  class="flex-1 rounded-md px-3 py-1.5 font-medium transition"
                  :class="
                    kbFormSource === 'markdown'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="kbFormSource = 'markdown'"
                >
                  {{ t("projects.kb.source.markdown") }}
                </button>
                <button
                  type="button"
                  class="flex-1 rounded-md px-3 py-1.5 font-medium transition"
                  :class="
                    kbFormSource === 'link'
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : 'text-slate-600 hover:text-slate-900'
                  "
                  @click="kbFormSource = 'link'"
                >
                  {{ t("projects.kb.source.link") }}
                </button>
              </div>
            </div>
          </div>

          <div v-if="kbFormSource === 'link'">
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.kb.modal.urlLabel") }}
            </label>
            <input
              v-model="kbFormExternalUrl"
              type="url"
              :required="kbFormSource === 'link'"
              :placeholder="t('projects.kb.modal.urlPlaceholder')"
              class="input-field font-mono text-xs"
            />
          </div>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.kb.modal.contentLabel") }}
            </label>
            <textarea
              v-model="kbFormContent"
              rows="7"
              :placeholder="t('projects.kb.modal.contentPlaceholder')"
              class="input-field font-mono text-xs leading-relaxed"
              data-testid="kb-form-content"
            />
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.kb.modal.tagsLabel") }}
              </label>
              <input
                v-model="kbFormTags"
                type="text"
                :placeholder="t('projects.kb.modal.tagsPlaceholder')"
                class="input-field text-xs"
                data-testid="kb-form-tags"
              />
            </div>

            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.kb.modal.endpointsLabel") }}
              </label>
              <input
                v-model="kbFormEndpoints"
                type="text"
                :placeholder="t('projects.kb.modal.endpointsPlaceholder')"
                class="input-field font-mono text-xs"
                data-testid="kb-form-endpoints"
              />
            </div>
          </div>

          <div class="flex items-center justify-end gap-2 border-t border-slate-100 pt-4">
            <button type="button" class="btn-secondary" @click="showKbDocModal = false">
              {{ t("common.cancel") }}
            </button>
            <button
              type="submit"
              class="btn-primary"
              :disabled="savingKbDoc"
              data-testid="kb-form-submit"
            >
              {{ savingKbDoc ? t("projects.kb.modal.saving") : t("projects.kb.modal.save") }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- Upload Knowledge File Modal -->
    <div
      v-if="showKbUploadModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
      data-testid="kb-upload-modal"
      @click.self="showKbUploadModal = false"
    >
      <div class="w-full max-w-lg overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
        <div class="flex items-center justify-between border-b border-slate-100 px-6 py-4">
          <h3 class="text-base font-semibold text-slate-900">{{ t("projects.kb.upload.title") }}</h3>
          <button
            type="button"
            class="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            @click="showKbUploadModal = false"
          >
            <X class="h-4 w-4" />
          </button>
        </div>

        <form class="space-y-4 px-6 py-5" @submit.prevent="handleUploadKbFile">
          <p class="text-xs leading-relaxed text-slate-500">
            {{ t("projects.kb.upload.desc") }}
          </p>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.kb.upload.fileLabel") }}
            </label>
            <input
              type="file"
              required
              accept=".md,.txt,.yaml,.yml,.json,.pdf,.docx,.csv,.http"
              class="block w-full cursor-pointer rounded-lg border border-slate-200 bg-slate-50 text-xs text-slate-700 file:mr-3 file:border-0 file:bg-indigo-50 file:px-3 file:py-2 file:text-xs file:font-semibold file:text-indigo-700 hover:file:bg-indigo-100"
              @change="onKbFileSelected"
            />
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.kb.modal.titleLabel").replace(" *", "") }}
                <span class="font-normal text-slate-400">{{ t("projects.kb.upload.titleHint") }}</span>
              </label>
              <input
                v-model="kbUploadTitle"
                type="text"
                :placeholder="kbUploadFile?.name || t('projects.kb.modal.titlePlaceholder')"
                class="input-field text-xs"
              />
            </div>

            <div>
              <label class="mb-1 block text-xs font-medium text-slate-700">
                {{ t("projects.kb.modal.typeLabel") }}
              </label>
              <select v-model="kbUploadType" class="input-field text-xs">
                <option v-for="dt in kbDocTypes" :key="dt" :value="dt">
                  {{ docTypeLabel(dt) }}
                </option>
              </select>
            </div>
          </div>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.kb.modal.tagsLabel") }}
            </label>
            <input
              v-model="kbUploadTags"
              type="text"
              :placeholder="t('projects.kb.modal.tagsPlaceholder')"
              class="input-field text-xs"
            />
          </div>

          <div>
            <label class="mb-1 block text-xs font-medium text-slate-700">
              {{ t("projects.kb.modal.endpointsLabel") }}
            </label>
            <input
              v-model="kbUploadEndpoints"
              type="text"
              :placeholder="t('projects.kb.modal.endpointsPlaceholder')"
              class="input-field font-mono text-xs"
            />
          </div>

          <div class="flex items-center justify-end gap-2 border-t border-slate-100 pt-4">
            <button type="button" class="btn-secondary" @click="showKbUploadModal = false">
              {{ t("common.cancel") }}
            </button>
            <button
              type="submit"
              class="btn-primary"
              :disabled="uploadingKbFile || !kbUploadFile"
            >
              <Upload class="h-3.5 w-3.5" />
              <span>
                {{
                  uploadingKbFile ? t("projects.kb.upload.uploading") : t("projects.kb.upload.submit")
                }}
              </span>
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- Preview Knowledge Doc Modal -->
    <div
      v-if="previewDoc"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
      data-testid="kb-preview-modal"
      @click.self="previewDoc = null"
    >
      <div class="w-full max-w-2xl overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
        <div class="flex items-start justify-between gap-3 border-b border-slate-100 px-6 py-4">
          <div class="min-w-0 space-y-1">
            <div class="flex flex-wrap items-center gap-2">
              <h3 class="text-base font-bold text-slate-900">{{ previewDoc.title }}</h3>
              <span
                class="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset"
                :class="docTypeBadgeClass(previewDoc.doc_type)"
              >
                {{ docTypeLabel(previewDoc.doc_type) }}
              </span>
              <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
                {{ docSourceLabel(previewDoc.source_kind) }}
              </span>
            </div>
            <p class="text-[11px] text-slate-400">
              {{ t("projects.kb.updatedAt", { date: formatDate(previewDoc.updated_at) }) }}
              <span v-if="previewDoc.size_bytes > 0"> · {{ formatBytes(previewDoc.size_bytes) }}</span>
            </p>
          </div>
          <button
            type="button"
            class="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            @click="previewDoc = null"
          >
            <X class="h-4 w-4" />
          </button>
        </div>

        <div class="max-h-[70vh] space-y-4 overflow-y-auto px-6 py-5">
          <div v-if="previewDoc.external_url" class="rounded-lg border border-indigo-200 bg-indigo-50/50 px-3.5 py-2.5">
            <a
              :href="previewDoc.external_url"
              target="_blank"
              rel="noopener noreferrer"
              class="inline-flex items-center gap-1.5 font-mono text-xs text-indigo-700 hover:underline"
            >
              <span>{{ previewDoc.external_url }}</span>
              <ExternalLink class="h-3.5 w-3.5" />
            </a>
          </div>

          <div v-if="previewDoc.tags.length > 0" class="flex flex-wrap items-center gap-1.5">
            <span class="text-[11px] font-semibold text-slate-500">
              {{ t("projects.kb.preview.tags") }}:
            </span>
            <span
              v-for="tag in previewDoc.tags"
              :key="tag"
              class="rounded-md bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600"
            >
              #{{ tag }}
            </span>
          </div>

          <div v-if="previewDoc.related_endpoints.length > 0" class="flex flex-wrap items-center gap-1.5">
            <span class="text-[11px] font-semibold text-slate-500">
              {{ t("projects.kb.preview.relatedEndpoints") }}:
            </span>
            <span
              v-for="ep in previewDoc.related_endpoints"
              :key="ep"
              class="rounded-md bg-indigo-50 px-2 py-0.5 font-mono text-[11px] text-indigo-700 ring-1 ring-inset ring-indigo-600/15"
            >
              {{ ep }}
            </span>
          </div>

          <pre
            v-if="previewDoc.content"
            class="whitespace-pre-wrap break-words rounded-xl border border-slate-200 bg-slate-50 p-4 font-mono text-xs leading-relaxed text-slate-800"
          >{{ previewDoc.content }}</pre>
          <p v-else class="rounded-xl border border-dashed border-slate-200 p-4 text-xs text-slate-500">
            {{ t("projects.kb.preview.noContent") }}
          </p>
        </div>

        <div class="flex items-center justify-between gap-2 border-t border-slate-100 bg-slate-50/70 px-6 py-3.5">
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="btn-secondary !py-1.5 !text-xs"
              @click="openEditKbModal(previewDoc)"
            >
              <Pencil class="h-3.5 w-3.5" />
              <span>{{ t("projects.kb.editBtn") }}</span>
            </button>
            <a
              v-if="previewDoc.source_kind !== 'link' && selectedProject"
              :href="knowledgeDownloadUrl(selectedProject.id, previewDoc.id)"
              download
              class="btn-secondary !py-1.5 !text-xs"
            >
              <Download class="h-3.5 w-3.5" />
              <span>{{ t("projects.kb.downloadBtn") }}</span>
            </a>
          </div>
          <button
            type="button"
            class="btn-danger !py-1.5 !text-xs"
            @click="handleDeleteKbDoc(previewDoc.id)"
          >
            <Trash2 class="h-3.5 w-3.5" />
            <span>{{ t("common.delete") }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
