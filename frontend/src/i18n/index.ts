import { reactive, ref, watch } from "vue";

export type Locale = "zh" | "en";

const STORAGE_KEY = "reconqa_locale";

function detectInitialLocale(): Locale {
  if (typeof window !== "undefined") {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved === "zh" || saved === "en") {
        return saved;
      }
    } catch {
      // Ignore storage access errors in restricted contexts
    }
    const navLang = (window.navigator?.language || "").toLowerCase();
    if (navLang.startsWith("zh")) {
      return "zh";
    }
  }
  return "en";
}

function syncDocumentLang(loc: Locale): void {
  if (typeof document !== "undefined") {
    document.documentElement.lang = loc === "zh" ? "zh-CN" : "en";
  }
}

export const locale = ref<Locale>(detectInitialLocale());

syncDocumentLang(locale.value);

watch(locale, (next) => {
  syncDocumentLang(next);
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Ignore storage errors
    }
  }
});

const dictionaries = reactive<{
  zh: Record<string, string>;
  en: Record<string, string>;
}>({
  zh: {},
  en: {},
});

export function registerMessages(bundle: {
  zh: Record<string, string>;
  en: Record<string, string>;
}): void {
  Object.assign(dictionaries.zh, bundle.zh);
  Object.assign(dictionaries.en, bundle.en);
}

export function getRegisteredDictionaries(): {
  zh: Record<string, string>;
  en: Record<string, string>;
} {
  return {
    zh: { ...dictionaries.zh },
    en: { ...dictionaries.en },
  };
}

export function setLocale(next: Locale): void {
  locale.value = next;
}

export function toggleLocale(): void {
  locale.value = locale.value === "zh" ? "en" : "zh";
}

export function t(key: string, params?: Record<string, string | number>): string {
  const current = locale.value;
  let template = dictionaries[current][key] ?? dictionaries.en[key] ?? dictionaries.zh[key] ?? key;
  if (params) {
    for (const [paramKey, paramValue] of Object.entries(params)) {
      template = template.replace(new RegExp(`\\{${paramKey}\\}`, "g"), String(paramValue));
    }
  }
  return template;
}

export function useI18n() {
  return {
    locale,
    setLocale,
    toggleLocale,
    t,
  };
}
