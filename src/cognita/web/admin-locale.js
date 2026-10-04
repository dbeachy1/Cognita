"use strict";

(function () {
  const locales = ["en-US", "es-ES", "fr-FR", "de-DE", "it-IT", "pt-BR"];
  const labels = {
    "en-US": "English (US)", "es-ES": "Español", "fr-FR": "Français",
    "de-DE": "Deutsch", "it-IT": "Italiano", "pt-BR": "Português (Brasil)",
  };
  const regionless = { es: "es-ES", fr: "fr-FR", de: "de-DE", it: "it-IT" };
  const cookieLocale = () => {
    const item = document.cookie.split(";").map((part) => part.trim())
      .find((part) => part.startsWith("cognita_lang="));
    if (!item) return "";
    try { return decodeURIComponent(item.slice("cognita_lang=".length)); }
    catch { return ""; }
  };
  const resolve = (value) => {
    const normalized = String(value || "").trim().replaceAll("_", "-");
    if (locales.includes(normalized)) return normalized;
    return regionless[normalized.toLowerCase()] || "en-US";
  };
  const choose = () => {
    const cookie = cookieLocale();
    if (locales.includes(cookie) || Object.hasOwn(regionless, cookie.toLowerCase())) return resolve(cookie);
    for (const language of navigator.languages || [navigator.language]) {
      const normalized = String(language || "").trim().replaceAll("_", "-");
      if (locales.includes(normalized)) return normalized;
      const mapped = regionless[normalized.toLowerCase()];
      if (mapped) return mapped;
    }
    return "en-US";
  };
  let locale = choose();
  const format = (message, values) => {
    const names = [...String(message).matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map((match) => match[1]);
    const expected = new Set(names);
    const supplied = new Set(Object.keys(values || {}));
    if (expected.size !== supplied.size || [...expected].some((name) => !supplied.has(name))) {
      throw new Error("Translation values do not match named placeholders");
    }
    return String(message).replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_all, name) => String(values[name]));
  };
  const api = {
    locale,
    labels,
    has(id) {
      return typeof id === "string" && Boolean(
        (api.catalog && Object.hasOwn(api.catalog, id)) ||
        (api.english && Object.hasOwn(api.english, id))
      );
    },
    t(id, values) {
      const message = (api.catalog && api.catalog[id]) || (api.english && api.english[id]);
      if (message === undefined) return id;
      return format(message, values);
    },
    number(value, options) { return new Intl.NumberFormat(locale, options).format(value); },
    date(value, options) { return new Intl.DateTimeFormat(locale, options).format(value); },
    setLocale(value) {
      const selected = resolve(value);
      document.cookie = `cognita_lang=${encodeURIComponent(selected)}; Path=/; SameSite=Lax${location.protocol === "https:" ? "; Secure" : ""}`;
      location.reload();
    },
    apply(root = document) {
      root.documentElement && (root.documentElement.lang = locale);
      root.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = api.t(node.dataset.i18n); });
      root.querySelectorAll("[data-i18n-attr]").forEach((node) => {
        const [attribute, id] = node.dataset.i18nAttr.split(":", 2);
        if (["title", "aria-label", "placeholder"].includes(attribute)) node.setAttribute(attribute, api.t(id));
      });
      root.querySelectorAll("[data-language-picker]").forEach((select) => {
        select.replaceChildren(...locales.map((tag) => {
          const option = document.createElement("option");
          option.value = tag;
          option.textContent = labels[tag];
          option.selected = tag === locale;
          return option;
        }));
        select.setAttribute("aria-label", api.t("admin.language.label"));
        select.addEventListener("change", () => api.setLocale(select.value), { once: true });
      });
    },
  };
  api.ready = (async () => {
    const version = encodeURIComponent(document.documentElement.dataset.version || "");
    let response;
    try {
      response = await fetch(`/static/locales/${locale}.json?v=${version}`, { cache: "force-cache" });
      if (!response.ok) throw new Error(`Could not load ${locale} message catalog`);
      api.catalog = await response.json();
    } catch (error) {
      if (locale === "en-US") throw error;
      locale = "en-US";
      api.locale = locale;
      response = await fetch(`/static/locales/en-US.json?v=${version}`, { cache: "force-cache" });
      if (!response.ok) throw new Error("Could not load the English message catalog");
      api.catalog = await response.json();
    }
    if (locale === "en-US") api.english = api.catalog;
    else {
      const english = await fetch(`/static/locales/en-US.json?v=${version}`, { cache: "force-cache" });
      api.english = english.ok ? await english.json() : {};
    }
    api.apply();
    return api;
  })();
  window.CognitaAdminLocale = api;
})();
