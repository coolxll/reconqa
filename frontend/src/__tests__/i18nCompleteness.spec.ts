import { describe, expect, it } from "vitest";
import { getRegisteredDictionaries, setLocale, t } from "@/i18n";

// Import all locale modules to register every translation dictionary
import "@/i18n/locales/capabilities";
import "@/i18n/locales/common";
import "@/i18n/locales/dashboards";
import "@/i18n/locales/explore";
import "@/i18n/locales/runs";
import "@/i18n/locales/scenarios";
import "@/i18n/locales/workspace";

describe("i18n locale completeness & parity", () => {
  const { zh, en } = getRegisteredDictionaries();
  const zhKeys = Object.keys(zh).sort();
  const enKeys = Object.keys(en).sort();

  it("loads a substantial set of registered keys across all locale bundles", () => {
    expect(zhKeys.length).toBeGreaterThan(200);
    expect(enKeys.length).toBeGreaterThan(200);
  });

  it("maintains 100% key parity between zh and en dictionaries", () => {
    const zhSet = new Set(zhKeys);
    const enSet = new Set(enKeys);

    const missingInEn = zhKeys.filter((k) => !enSet.has(k));
    const missingInZh = enKeys.filter((k) => !zhSet.has(k));

    expect(missingInEn).toEqual([]);
    expect(missingInZh).toEqual([]);
    expect(zhKeys.length).toBe(enKeys.length);
  });

  it("has zero unexpected empty or whitespace-only translations in zh and en", () => {
    // Chinese counter classifier ("个") intentionally omitted in English (guarded by v-if in CapabilitiesView)
    const INTENTIONALLY_EMPTY_EN_KEYS = new Set(["cap.summary.coreUnit"]);

    const emptyZh = zhKeys.filter((k) => typeof zh[k] !== "string" || zh[k].trim().length === 0);
    const emptyEn = enKeys.filter(
      (k) =>
        !INTENTIONALLY_EMPTY_EN_KEYS.has(k) &&
        (typeof en[k] !== "string" || en[k].trim().length === 0)
    );

    expect(emptyZh).toEqual([]);
    expect(emptyEn).toEqual([]);
  });

  it("includes new deep assertion and chain runner keys in both locales and interpolates params", () => {
    setLocale("zh");
    expect(t("scen.assert.dbDsnPlaceholder")).toBe("ENV:RECONQA_DB_DSN");
    expect(t("corr.runner.resumeBtn", { step: 3 })).toContain("3");

    setLocale("en");
    expect(t("scen.assert.dbDsnPlaceholder")).toBe("ENV:RECONQA_DB_DSN");
    expect(t("corr.runner.resumeBtn", { step: 3 })).toBe("Resume from Step 3");
  });
});
