<script setup lang="ts">
import { computed } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";

const props = defineProps<{
  content: string;
}>();

const renderedHtml = computed(() => {
  const raw = props.content ?? "";
  if (!raw.trim()) return "";
  const parsed = marked.parse(raw, { gfm: true, breaks: true, async: false }) as string;
  const clean = DOMPurify.sanitize(parsed, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ["target", "rel"],
  });
  return clean.replace(
    /<a\s+(?![^>]*\btarget=)/gi,
    '<a target="_blank" rel="noopener noreferrer" '
  );
});
</script>

<template>
  <div class="md-body" data-testid="markdown-viewer" v-html="renderedHtml" />
</template>

<style scoped>
.md-body {
  color: #1e293b;
  font-size: 0.875rem;
  line-height: 1.7;
  word-break: break-word;
}

.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3),
.md-body :deep(h4) {
  color: #0f172a;
  font-weight: 700;
  line-height: 1.35;
  margin-top: 1.35em;
  margin-bottom: 0.6em;
}

.md-body :deep(h1:first-child),
.md-body :deep(h2:first-child),
.md-body :deep(h3:first-child) {
  margin-top: 0;
}

.md-body :deep(h1) {
  font-size: 1.375rem;
  padding-bottom: 0.35em;
  border-bottom: 1px solid #e2e8f0;
}

.md-body :deep(h2) {
  font-size: 1.15rem;
  padding-bottom: 0.3em;
  border-bottom: 1px solid #f1f5f9;
}

.md-body :deep(h3) {
  font-size: 1rem;
}

.md-body :deep(h4) {
  font-size: 0.9rem;
  color: #334155;
}

.md-body :deep(p) {
  margin-top: 0;
  margin-bottom: 0.85em;
}

.md-body :deep(strong) {
  font-weight: 600;
  color: #0f172a;
}

.md-body :deep(ul),
.md-body :deep(ol) {
  margin-top: 0;
  margin-bottom: 0.9em;
  padding-left: 1.5rem;
}

.md-body :deep(ul) {
  list-style-type: disc;
}

.md-body :deep(ol) {
  list-style-type: decimal;
}

.md-body :deep(li) {
  margin-bottom: 0.3em;
}

.md-body :deep(li > ul),
.md-body :deep(li > ol) {
  margin-top: 0.3em;
  margin-bottom: 0.3em;
}

.md-body :deep(blockquote) {
  margin: 0.9em 0;
  padding: 0.65rem 1rem;
  border-left: 4px solid #6366f1;
  background-color: #eef2ff;
  color: #312e81;
  border-radius: 0 0.5rem 0.5rem 0;
}

.md-body :deep(blockquote p:last-child) {
  margin-bottom: 0;
}

.md-body :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.8em;
  background-color: #f1f5f9;
  color: #4338ca;
  padding: 0.12rem 0.38rem;
  border-radius: 0.3rem;
  border: 1px solid #e2e8f0;
}

.md-body :deep(pre) {
  margin: 0.9em 0;
  padding: 0.9rem 1rem;
  background-color: #0f172a;
  color: #e2e8f0;
  border-radius: 0.65rem;
  overflow-x: auto;
  line-height: 1.55;
}

.md-body :deep(pre code) {
  background: transparent;
  color: inherit;
  padding: 0;
  border: none;
  font-size: 0.78rem;
}

.md-body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 1em 0;
  font-size: 0.8125rem;
  display: block;
  overflow-x: auto;
}

.md-body :deep(thead) {
  background-color: #f8fafc;
}

.md-body :deep(th),
.md-body :deep(td) {
  border: 1px solid #e2e8f0;
  padding: 0.5rem 0.75rem;
  text-align: left;
  vertical-align: top;
}

.md-body :deep(th) {
  font-weight: 600;
  color: #334155;
  white-space: nowrap;
}

.md-body :deep(tbody tr:nth-child(even)) {
  background-color: #f8fafc;
}

.md-body :deep(hr) {
  border: none;
  border-top: 1px solid #e2e8f0;
  margin: 1.25em 0;
}

.md-body :deep(a) {
  color: #4f46e5;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.md-body :deep(a:hover) {
  color: #3730a3;
}
</style>
