import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../src/cognita/web/admin-locale.js", import.meta.url), "utf8");
const catalogs = {
  "fr-FR": { "admin.language.label": "Langue", "test.message": "Dossier {folder}" },
  "en-US": { "admin.language.label": "Language", "test.message": "Folder {folder}" },
};

async function runtime({ cookie = "", languages = ["en-US"] } = {}) {
  const document = {
    cookie,
    documentElement: { dataset: { version: "test-version" }, lang: "en" },
    querySelectorAll: () => [],
  };
  const location = { protocol: "https:", reloaded: false, reload() { this.reloaded = true; } };
  const requests = [];
  const window = {};
  const context = {
    document,
    location,
    navigator: { languages, language: languages[0] },
    window,
    fetch: async (url) => {
      requests.push(url);
      const locale = url.match(/locales\/([^/?]+)\.json/)?.[1];
      return { ok: Boolean(catalogs[locale]), async json() { return catalogs[locale]; } };
    },
    Object, Intl, Set, Error, String, Promise, encodeURIComponent,
  };
  vm.runInNewContext(source, context);
  await window.CognitaAdminLocale.ready;
  return { document, location, requests, locale: window.CognitaAdminLocale };
}

test("locale cookie wins, sets html lang, formats named values, and uses a versioned URL", async () => {
  const result = await runtime({ cookie: "cognita_lang=fr-FR", languages: ["pt-BR"] });
  assert.equal(result.locale.locale, "fr-FR");
  assert.equal(result.document.documentElement.lang, "fr-FR");
  assert.equal(result.locale.t("test.message", { folder: "Docs" }), "Dossier Docs");
  assert.match(result.requests[0], /fr-FR\.json\?v=test-version$/);
  assert.throws(() => result.locale.t("test.message", {}), /named placeholders/);
  result.locale.setLocale("pt-BR");
  assert.match(result.document.cookie, /cognita_lang=pt-BR/);
  assert.match(result.document.cookie, /Secure/);
  assert.equal(result.location.reloaded, true);
});

test("regionless supported tags map exactly; unsupported regions resolve through browser fallback", async () => {
  const regionless = await runtime({ languages: ["fr"] });
  assert.equal(regionless.locale.locale, "fr-FR");
  const unsupported = await runtime({ cookie: "cognita_lang=pt-PT", languages: ["fr-CA", "en-US"] });
  assert.equal(unsupported.locale.locale, "en-US");
});
