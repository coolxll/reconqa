<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  AlertTriangle,
  BookOpen,
  Check,
  CheckCircle2,
  Copy,
  Download,
  ExternalLink,
  Eye,
  FileCode2,
  FileText,
  Link2,
  Pencil,
  Plus,
  Search,
  Tag,
  Trash2,
  Upload,
  X,
} from "lucide-vue-next";
import {
  createKnowledgeDoc,
  deleteKnowledgeDoc,
  knowledgeDownloadUrl,
  listKnowledgeDocs,
  updateKnowledgeDoc,
  uploadKnowledgeDoc,
} from "@/api/projects";
import { copyToClipboard } from "@/utils/clipboard";
import type {
  KnowledgeDoc,
  KnowledgeDocSourceKind,
  KnowledgeDocType,
} from "@/api/types";
import EmptyState from "@/components/layout/EmptyState.vue";
import MarkdownViewer from "@/components/common/MarkdownViewer.vue";
import { useWorkspace } from "@/stores/workspace";
import { useI18n } from "@/i18n";

const route = useRoute();
const { locale, t } = useI18n();
const { activeProjectId } = useWorkspace();

const projectId = computed(() =>
  typeof route.params.projectId === "string" && route.params.projectId
    ? route.params.projectId
    : activeProjectId.value
);

const knowledgeDocs = ref<KnowledgeDoc[]>([]);
const loading = ref(true);
const bannerError = ref<string | null>(null);
const bannerSuccess = ref<string | null>(null);

const kbCategoryFilter = ref<"all" | KnowledgeDocType>("all");
const kbSearch = ref("");

// Create / Edit Modal
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

// Upload Modal
const showKbUploadModal = ref(false);
const uploadingKbFile = ref(false);
const kbUploadFile = ref<File | null>(null);
const kbUploadTitle = ref("");
const kbUploadType = ref<KnowledgeDocType>("prd");
const kbUploadTags = ref("");
const kbUploadEndpoints = ref("");

// Preview Modal
const previewDoc = ref<KnowledgeDoc | null>(null);
const previewMode = ref<"rendered" | "raw">("rendered");
const copiedMarkdown = ref(false);

function openPreview(doc: KnowledgeDoc): void {
  previewDoc.value = doc;
  previewMode.value = "rendered";
  copiedMarkdown.value = false;
}

async function copyPreviewMarkdown(): Promise<void> {
  const text = previewDoc.value?.content;
  if (!text) return;
  const ok = await copyToClipboard(text);
  if (!ok) return;
  copiedMarkdown.value = true;
  window.setTimeout(() => {
    copiedMarkdown.value = false;
  }, 1800);
}

function stripMarkdown(raw: string): string {
  if (!raw) return "";
  return raw
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*[-*_]{3,}\s*$/gm, " ")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+\.\s+/gm, "")
    .replace(/\|/g, " ")
    .replace(/[*_~]{1,3}/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

const kbDocTypes: KnowledgeDocType[] = [
  "prd",
  "api_spec",
  "business_rule",
  "test_notes",
  "architecture",
  "other",
];

const filteredKnowledgeDocs = computed(() => {
  const typeFilter = kbCategoryFilter.value;
  const q = kbSearch.value.trim().toLowerCase();
  return knowledgeDocs.value.filter((doc) => {
    if (typeFilter !== "all" && doc.doc_type !== typeFilter) return false;
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
    if (bannerSuccess.value === msg) bannerSuccess.value = null;
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

async function loadDocs(): Promise<void> {
  if (!projectId.value) {
    knowledgeDocs.value = [];
    loading.value = false;
    return;
  }
  loading.value = true;
  previewDoc.value = null;
  try {
    knowledgeDocs.value = await listKnowledgeDocs(projectId.value);
  } catch (err) {
    notifyError(err, t("projects.kb.err.load"));
  } finally {
    loading.value = false;
  }
}

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
  if (!projectId.value || !kbFormTitle.value.trim()) return;
  savingKbDoc.value = true;
  try {
    const tags = parseCsvList(kbFormTags.value);
    const related_endpoints = parseCsvList(kbFormEndpoints.value);
    if (editingDocId.value) {
      const updated = await updateKnowledgeDoc(projectId.value, editingDocId.value, {
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
      const created = await createKnowledgeDoc(projectId.value, {
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
  if (!projectId.value || !kbUploadFile.value) return;
  uploadingKbFile.value = true;
  try {
    const created = await uploadKnowledgeDoc(projectId.value, kbUploadFile.value, {
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
  if (!projectId.value) return;
  try {
    await deleteKnowledgeDoc(projectId.value, docId);
    knowledgeDocs.value = knowledgeDocs.value.filter((d) => d.id !== docId);
    if (previewDoc.value?.id === docId) previewDoc.value = null;
    notifySuccess(t("projects.kb.toast.deleted"));
  } catch (err) {
    notifyError(err, t("projects.kb.err.delete"));
  }
}

watch(projectId, () => {
  void loadDocs();
});

onMounted(() => {
  void loadDocs();
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

    <!-- Banner & Primary Actions -->
    <div class="rounded-xl border border-indigo-200/80 bg-indigo-50/50 p-4 text-xs text-indigo-950">
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
    <div class="card space-y-3 p-4">
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
      <div v-if="loading" class="p-6 text-center text-xs text-slate-500">
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
            <button type="button" class="btn-secondary !text-xs" @click="openUploadKbModal">
              <Upload class="h-3.5 w-3.5" />
              <span>{{ t("projects.kb.uploadBtn") }}</span>
            </button>
            <button type="button" class="btn-primary !text-xs" @click="openCreateKbModal">
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
                  @click="openPreview(doc)"
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

              <p v-if="doc.content" class="line-clamp-2 text-xs leading-relaxed text-slate-600">
                {{ stripMarkdown(doc.content) }}
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
                @click="openPreview(doc)"
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
                v-if="doc.source_kind !== 'link' && projectId"
                :href="knowledgeDownloadUrl(projectId, doc.id)"
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
            <button type="submit" class="btn-primary" :disabled="uploadingKbFile || !kbUploadFile">
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

    <!-- Preview Modal -->
    <div
      v-if="previewDoc"
      class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm"
      data-testid="kb-preview-modal"
      @click.self="previewDoc = null"
    >
      <div class="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
        <div class="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 px-6 py-4">
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

          <div class="flex shrink-0 items-center gap-2">
            <!-- Rendered | Raw Toggle -->
            <div
              v-if="previewDoc.content"
              data-testid="kb-preview-mode-toggle"
              class="inline-flex items-center rounded-lg border border-slate-200 bg-slate-100 p-0.5 text-xs font-medium"
            >
              <button
                type="button"
                data-testid="kb-preview-mode-rendered"
                class="inline-flex items-center gap-1 rounded-md px-2.5 py-1 transition"
                :class="
                  previewMode === 'rendered'
                    ? 'bg-white text-indigo-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900'
                "
                @click="previewMode = 'rendered'"
              >
                <Eye class="h-3.5 w-3.5" />
                <span>{{ t("kb.previewRendered") }}</span>
              </button>
              <button
                type="button"
                data-testid="kb-preview-mode-raw"
                class="inline-flex items-center gap-1 rounded-md px-2.5 py-1 transition"
                :class="
                  previewMode === 'raw'
                    ? 'bg-white text-indigo-700 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900'
                "
                @click="previewMode = 'raw'"
              >
                <FileCode2 class="h-3.5 w-3.5" />
                <span>{{ t("kb.previewRaw") }}</span>
              </button>
            </div>

            <button
              v-if="previewDoc.content"
              type="button"
              data-testid="kb-copy-markdown-btn"
              class="btn-secondary !px-2.5 !py-1.5 !text-xs"
              @click="copyPreviewMarkdown"
            >
              <Check v-if="copiedMarkdown" class="h-3.5 w-3.5 text-emerald-600" />
              <Copy v-else class="h-3.5 w-3.5" />
              <span>{{ copiedMarkdown ? t("kb.copiedMarkdown") : t("kb.copyMarkdown") }}</span>
            </button>

            <button
              type="button"
              class="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              @click="previewDoc = null"
            >
              <X class="h-4 w-4" />
            </button>
          </div>
        </div>

        <div class="flex-1 space-y-4 overflow-y-auto px-6 py-5">
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

          <template v-if="previewDoc.content">
            <div
              v-if="previewMode === 'rendered'"
              class="rounded-xl border border-slate-200 bg-white p-5"
            >
              <MarkdownViewer :content="previewDoc.content" />
            </div>
            <pre
              v-else
              data-testid="kb-preview-raw-content"
              class="whitespace-pre-wrap break-words rounded-xl border border-slate-200 bg-slate-50 p-4 font-mono text-xs leading-relaxed text-slate-800"
            >{{ previewDoc.content }}</pre>
          </template>
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
              v-if="previewDoc.source_kind !== 'link' && projectId"
              :href="knowledgeDownloadUrl(projectId, previewDoc.id)"
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
