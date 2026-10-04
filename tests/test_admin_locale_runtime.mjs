import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../src/cognita/web/admin-locale.js", import.meta.url), "utf8");
const catalogs = {
  "fr-FR": {
    "admin.language.label": "Langue", "admin.action.failed": "Échec de l’action.",
    "admin.technical_detail": "Détail technique", "test.message": "Dossier {folder}",
    "test.known": "Le dossier est introuvable.",
  },
  "en-US": {
    "admin.language.label": "Language", "admin.action.failed": "The action could not be completed.",
    "admin.technical_detail": "Technical detail", "test.message": "Folder {folder}",
    "test.known": "The folder could not be found.",
  },
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

test("locale reports whether a presentation ID is mapped", async () => {
  const result = await runtime({ cookie: "cognita_lang=fr-FR" });
  assert.equal(result.locale.has("test.known"), true);
  assert.equal(result.locale.has("test.missing"), false);
});

test("Admin presents unknown outcomes with a localized heading and labeled technical detail", async () => {
  const result = await runtime({ cookie: "cognita_lang=fr-FR" });
  const app = await readFile(new URL("../src/cognita/web/app.js", import.meta.url), "utf8");
  const start = app.indexOf("function outcomeParts(");
  const end = app.indexOf("\n}", app.indexOf("function presentOutcome(", start)) + 2;
  assert.ok(start >= 0 && end > start, "outcome presentation helpers must remain present");
  const context = { window: { CognitaAdminLocale: result.locale } };
  vm.runInNewContext(app.slice(start, end), context);

  assert.deepEqual(
    JSON.parse(JSON.stringify(context.outcomeParts({ detail: "Project already exists" }, "admin.action.failed"))),
    { message: "Échec de l’action.", technicalDetail: "Project already exists" },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.outcomeParts({ presentation_id: "admin.project.future_error", detail: "duplicate" }, "admin.action.failed"))),
    { message: "Échec de l’action.", technicalDetail: "admin.project.future_error — duplicate" },
  );
  assert.equal(
    context.presentOutcome({ detail: "Project already exists" }, "admin.action.failed"),
    "Échec de l’action.\n\nDétail technique: Project already exists",
  );
  assert.equal(
    context.presentOutcome({ presentation_id: "admin.project.future_error", detail: "duplicate" }, "admin.action.failed"),
    "Échec de l’action.\n\nDétail technique: admin.project.future_error — duplicate",
  );
  assert.equal(
    context.presentOutcome({ presentation_id: "test.known", detail: "ignored raw detail" }, "admin.action.failed"),
    "Le dossier est introuvable.",
  );

  const html = await readFile(new URL("../src/cognita/web/index.html", import.meta.url), "utf8");
  assert.match(html, /<details x-show="technicalDetail"><summary x-text="technicalDetailLabel"><\/summary><p x-text="technicalDetail"/);
});
