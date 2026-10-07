// Cognita 12.4 admin UI — acceleration, immutable connector entities, policies, and credentials.
"use strict";

const $ = (sel) => document.querySelector(sel);
const t = (id, values) => window.CognitaAdminLocale.t(id, values);
// Shared, non-persistent Admin cache. Panel migrations can consume this
// object without creating another copy of projects or policy data.
const adminState = window.CognitaAdminState
  ? CognitaAdminState.createAdminState()
  : null;
const state = {
  projects: [],
  connectors: [],
  connectorsRevision: 0,
  workspaceConnectors: [],
  workspaceConnectorsRevision: 0,
  workspaces: [],
  workspacesRevision: 0,
  workspaceRuntime: {},
  workspaceSettings: {},
  workspaceNetworkStoredRules: [],
  workspaceNetworkEditorRules: [],
  workspaceNetworkLegacyOverrides: new Set(),
  workspaceNetworkEditorBaseline: "[]",
  workspaceCursor: null,
  workspaceCursorStack: [],
  workspaceNextCursor: null,
  version: "",
  gpuAcceleration: {},
  editingConnectorId: null,
  selectedConnectorId: null,
  lastExistingConnectorId: null,
  connectorDrafts: new Map(),
  connectorSettingsDrafts: new Map(),
  connectorView: "settings",
  connectorFunctions: new Map(),
  connectorCreateDraft: null,
  highlightedProject: null,
};
let editingProjectSettingsName = null;

function routeForHash(hash) {
  return CognitaAdminState.parseRoute(hash || window.location.hash);
}

function normalizedConnectorRoute(route) {
  if (!route || route.top !== "connectors" || !window.CognitaAdminState) return route;
  // Preserve every non-Add connector route until the connector slice resolves.
  // This includes compatibility aliases without an ID: normalizing #connectors
  // against an as-yet-empty list would incorrectly rewrite it to Add connector.
  if (route.sub !== "add" && !state.connectors.length &&
      (!adminState || adminState.connectors.status !== "ready")) return { ...route, pending: true };
  return CognitaAdminState.normalizeConnectorRoute(route, state.connectors);
}

function connectorRoute(connectorId, sub = "settings") {
  return { top: "connectors", connectorId: connectorId == null ? null : connectorId, sub };
}

function selectShellTab(buttons, route) {
  buttons.forEach((button) => {
    // Primary buttons represent a section, so their selected state must not
    // depend on which secondary route is active inside that section.
    const active = button.id.startsWith("tab-")
      ? route.top === button.id.replace(/^tab-/, "").split("-")[0]
      : button.dataset.route === CognitaAdminState.routeFragment(route);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
}

function renderShellRoute(route, focusPanel = false) {
  if (!route) return;
  const top = document.querySelectorAll("body > main .admin-tabs:not(.admin-secondary-tabs):not(.admin-subtabs) [role=tab]");
  selectShellTab(top, route);
  const projectTabs = document.querySelectorAll("#panel-projects > .admin-subtabs [role=tab]");
  const connectorTabs = document.querySelectorAll("#connector-tabs [role=tab]");
  const workspaceTabs = document.querySelectorAll("#workspace-tabs [role=tab]");
  selectShellTab(projectTabs, route);
  selectShellTab(connectorTabs, route);
  selectShellTab(workspaceTabs, route);
  document.querySelectorAll(".admin-panel").forEach((panel) => {
    const active = panel.id === `panel-${route.top}`;
    panel.hidden = !active;
    panel.setAttribute("aria-hidden", active ? "false" : "true");
  });
  const projectsView = $("#projects-section");
  const projectsCreate = $("#add-project-section");
  if (projectsView && projectsCreate) {
    projectsView.hidden = route.top !== "projects" || route.sub === "create";
    projectsCreate.hidden = route.top !== "projects" || route.sub !== "create";
  }
  const connectorPanels = {
    setup: $("#connector-setup-panel"), clients: $("#connector-clients-panel"),
    access: $("#connector-access-panel"), transfer: $("#connector-transfer-panel"),
  };
  Object.entries(connectorPanels).forEach(([name, panel]) => {
    if (panel) panel.hidden = route.top !== "connectors" ||
      (route.sub === "add" || route.sub === "settings" ? name !== "setup" : route.sub !== name);
  });
  const entityTabs = $("#connector-entity-tabs");
  const functionTabs = $("#connector-tabs");
  const hasConnector = Boolean(route.top === "connectors" && route.connectorId && selectedConnector());
  if (entityTabs) entityTabs.hidden = route.top !== "connectors";
  if (functionTabs) functionTabs.hidden = route.top !== "connectors" || !hasConnector;
  const settingsDetails = $("#connector-settings-details");
  if (settingsDetails) settingsDetails.hidden = !hasConnector || route.sub !== "settings";
  const workspacePanels = {
    runtime: $("#workspace-runtime-panel"), policy: $("#workspace-policy-panel"),
    connectors: $("#workspace-connectors-panel"),
  };
  Object.entries(workspacePanels).forEach(([name, panel]) => {
    if (panel) panel.hidden = route.top !== "workspaces" || route.sub !== name;
  });
  if (focusPanel) {
    const heading = route.top === "projects" && route.sub === "create" ?
      $("#add-project-section header") : $( `#panel-${route.top} article header` );
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
  }
}

function navigateShell(route, replace = false, focus = true) {
  captureConnectorCreateDraft();
  captureConnectorDraft();
  captureConnectorSettingsDraft();
  if (route && route.top === "connectors") route = normalizedConnectorRoute(route);
  const fragment = route?.pending ? "#connectors" : CognitaAdminState.routeFragment(route);
  if (window.location.hash !== fragment) {
    if (replace) {
      window.history.replaceState(null, "", fragment);
      activateShellRoute(route, focus);
    } else {
      window.location.hash = fragment;
    }
    return;
  }
  activateShellRoute(route, focus);
}

function activateShellRoute(route, focus = false, revalidate = true) {
  if (route && route.top === "connectors") {
    route = normalizedConnectorRoute(route);
    if (route.sub === "add") {
      if (state.selectedConnectorId != null) state.lastExistingConnectorId = state.selectedConnectorId;
      state.selectedConnectorId = null;
    } else {
      state.selectedConnectorId = route.connectorId;
      state.lastExistingConnectorId = route.connectorId;
    }
    state.connectorView = route.sub === "add" ? "add" : route.sub;
    renderConnectorEntityTabs();
    renderConnectorFunctionTabs(route);
    if (route.sub !== "add") {
      const connector = selectedConnector();
      if (connector) {
        renderConnectorSettings(connector);
        renderConnectorEditor(connector);
        renderConnectorEditorProjects(connector);
      }
      if (route.sub === "clients" && adminState?.oauthGrants?.data) loadOAuth({ status: adminState.oauthStatus?.data, grants: adminState.oauthGrants.data });
    } else {
      renderConnectorCreate();
    }
  }
  renderShellRoute(route, focus);
  if (route.top === "authentication" && authState.global) {
    renderAuthentication({ revision: authState.revision, global: authState.global,
      projects: authState.projects, warnings: authState.warnings });
  }
  if (route.top === "workspaces" && adminState) {
    if (adminState.workspaces.data) renderWorkspaces(adminState.workspaces.data);
    if (adminState.workspaceRuntime.data) renderWorkspaceRuntime(adminState.workspaceRuntime.data);
    if (adminState.workspaceSettings.data) renderWorkspaceSettings(adminState.workspaceSettings.data);
    if (adminState.workspaceConnectors.data) renderWorkspaceConnectors(adminState.workspaceConnectors.data);
  }
  if (route.top === "settings" && adminState?.gpuAcceleration?.data) {
    renderGpuAcceleration(adminState.gpuAcceleration.data);
  }
  if (revalidate) revalidateShellRoute(route);
}

function initAdminShell() {
  if (!window.CognitaAdminState) return;
  const route = routeForHash(window.location.hash);
  if (!window.location.hash || window.location.hash !== CognitaAdminState.routeFragment(route)) {
    window.history.replaceState(null, "", CognitaAdminState.routeFragment(route));
  }
  renderShellRoute(route);
  const groups = [
    Array.from(document.querySelectorAll("body > main .admin-tabs:not(.admin-secondary-tabs):not(.admin-subtabs) [role=tab]")),
    Array.from(document.querySelectorAll("#panel-projects > .admin-subtabs [role=tab]")),
    Array.from(document.querySelectorAll("#connector-entity-tabs [role=tab]")),
    Array.from(document.querySelectorAll("#connector-tabs [role=tab]")),
    Array.from(document.querySelectorAll("#workspace-tabs [role=tab]")),
  ];
  const wire = (buttons) => buttons.forEach((button, index) => {
    button.addEventListener("click", () => navigateShell(routeForHash(button.dataset.route)));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End", "Enter", " "].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Enter" || event.key === " ") { button.click(); return; }
      const delta = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + delta + buttons.length) % buttons.length;
      buttons[next].focus();
    });
  });
  groups.forEach(wire);
  window.addEventListener("hashchange", () => {
    captureConnectorCreateDraft();
    captureConnectorDraft();
    captureConnectorSettingsDraft();
    const nextRoute = normalizedConnectorRoute(routeForHash(window.location.hash));
    const canonical = CognitaAdminState.routeFragment(nextRoute);
    if (!nextRoute.pending && window.location.hash !== canonical) window.history.replaceState(null, "", canonical);
    activateShellRoute(nextRoute, true);
  });
  window.adminNavigate = (value) => {
    const alias = typeof value === "string" && ["#connectors", "#connectors/setup", "#workspaces"].includes(value.split("?", 1)[0]);
    navigateShell(typeof value === "string" ? routeForHash(value) : value, alias);
  };
}

function openProjectSettings(project) {
  editingProjectSettingsName = project.name;
  $("#project-settings-name").textContent = project.name;
  $("#project-settings-exclude").checked = Boolean(project.exclude_from_default_permissions);
  $("#project-settings-dialog").showModal();
}

$("#project-settings-auth").addEventListener("click", () => {
  const name = editingProjectSettingsName;
  $("#project-settings-dialog").close();
  if (window.adminNavigate) window.adminNavigate("#authentication/" + encodeURIComponent(name));
});

async function saveProjectSettings(event) {
  event.preventDefault();
  const button = $("#project-settings-save");
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  try {
    await api("/api/projects/" + encodeURIComponent(editingProjectSettingsName), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        exclude_from_default_permissions: $("#project-settings-exclude").checked,
      }),
    });
    $("#project-settings-dialog").close();
    await refreshAdminMutation("project:update");
  } catch (err) {
    await uiNotice({ title: t("admin.projects.settings_save_failed"), body: err.message, technicalDetail: err.technicalDetail });
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

// ---- modal (Alpine) — promise-based replacement for confirm()/alert()/prompt() ----
// index.html holds the single <dialog id="modal">; app.js loads BEFORE
// alpine.min.js so this alpine:init listener is registered in time.
//
// uiForm (13.2.0) is the prompt() replacement: `fields` is a list of
// {name, label, type: "text"|"password"|"select", value, options, maxlength}
// and the result is {ok, values}. The five credential dialogs used the
// browser's raw prompt box until 13.2.0; the modal had no input, so those
// dialogs had not yet been converted.

document.addEventListener("alpine:init", () => {
  Alpine.data("modal", () => ({
    mode: "notice", title: "", body: "", technicalDetail: "", technicalDetailLabel: t("admin.technical_detail"), checkboxLabel: "", confirmLabel: t("admin.action.ok"), cancelLabel: t("admin.action.cancel"),
    danger: false, checked: false, fields: [], values: {}, _resolve: null,
    init() {
      window.uiConfirm = (opts) =>
        this._open({ mode: "confirm", confirmLabel: t("admin.action.confirm"), ...opts });
      window.uiNotice = (opts) =>
        this._open({ mode: "notice", confirmLabel: t("admin.action.ok"), technicalDetailLabel: t("admin.technical_detail"), ...opts });
      window.uiForm = (opts) =>
        this._open({
          mode: "form", confirmLabel: t("admin.action.ok"), ...opts,
          values: Object.fromEntries((opts.fields || []).map((field) => [field.name, field.value ?? ""])),
        });
    },
    _open(opts) {
      Object.assign(this, { checkboxLabel: "", danger: false, checked: false, technicalDetail: "", technicalDetailLabel: t("admin.technical_detail"), fields: [], values: {} }, opts);
      this.$refs.dlg.showModal();
      this.$nextTick(() => { const first = this.$refs.dlg.querySelector("input:not([type=checkbox]), select"); if (first) first.focus(); });
      return new Promise((resolve) => (this._resolve = resolve));
    },
    _close(result) {
      this.$refs.dlg.close();
      if (this._resolve) this._resolve(result);
      this._resolve = null;
      // A password typed into a field must not outlive its dialog.
      this.values = {};
      this.fields = [];
    },
    ok() { this._close({ ok: true, checked: this.checked, values: { ...this.values } }); },
    cancel() { this._close({ ok: false, checked: false, values: {} }); },  // also Esc, via @cancel
  }));
});

// Where a credential panel keeps its revision and where its list renders.
// The combined page has an outer panel and an inner list; the Workspace-only
// page uses one element for both. Revision state belongs on the panel because
// click handlers read it there.
function credentialPanelOf(element) {
  const panel = element.closest(".connector-credentials, .workspace-credentials") || element;
  return { panel, list: panel.querySelector(".connector-credentials-list") || panel };
}

async function askAdminPassword(title, body, confirmLabel) {
  const form = await uiForm({
    title, body, confirmLabel,
    fields: [{ name: "current_password", label: t("admin.credential.field.admin_password"), type: "password", maxlength: 512 }],
  });
  return form.ok ? (form.values.current_password || "") : "";
}

function csrfToken() {
  const entry = document.cookie.split(";").map((part) => part.trim())
    .find((part) => part.startsWith("cognita_csrf="));
  return entry ? decodeURIComponent(entry.slice("cognita_csrf=".length)) : "";
}

function outcomeParts(payload, outcome = "admin.action.completed") {
  const locale = window.CognitaAdminLocale;
  const presentationId = payload && typeof payload.presentation_id === "string"
    ? payload.presentation_id : "";
  if (presentationId && locale && locale.has(presentationId)) {
    try { return { message: locale.t(presentationId, payload.presentation_values || {}), technicalDetail: "" }; }
    catch { /* malformed or older response: retain the documented generic fallback */ }
  }
  const raw = payload && [payload.detail, payload.message, payload.error]
    .find((value) => typeof value === "string" && value.length > 0);
  const technical = [presentationId, raw].filter(Boolean).join(" — ");
  return { message: locale ? locale.t(outcome) : (raw || ""), technicalDetail: technical };
}

function presentOutcome(payload, outcome = "admin.action.completed") {
  const locale = window.CognitaAdminLocale;
  const { message, technicalDetail } = outcomeParts(payload, outcome);
  return technicalDetail && locale
    ? `${message}\n\n${locale.t("admin.technical_detail")}: ${technicalDetail}`
    : message;
}

function inlineErrorText(error) {
  return error.technicalDetail
    ? `${error.message}\n\n${t("admin.technical_detail")}: ${error.technicalDetail}`
    : error.message;
}

function formatBytes(bytes) {
  const locale = window.CognitaAdminLocale;
  const number = (value, options) => locale ? locale.number(value, options) : value.toLocaleString();
  if (bytes < 1024) return `${number(bytes)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${number(value, { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${units[unit]}`;
}

function formatMaybeBytes(value) {
  return value === null || value === undefined || value === "—" ||
    !Number.isFinite(Number(value)) ? window.CognitaAdminLocale.t("admin.status.unavailable") : formatBytes(Number(value));
}

async function api(path, opts = {}) {
  const method = String(opts.method || "GET").toUpperCase();
  const headers = new Headers(opts.headers || {});
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
  }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    // Session expired or missing — bounce to the login page ("/" serves it).
    window.location.href = "/";
    throw new Error(t("admin.login.session_expired"));
  }
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message || data.error)) || res.statusText;
    const presentation = outcomeParts(data || { detail: typeof msg === "string" ? msg : JSON.stringify(msg) }, "admin.action.failed");
    const error = new Error(presentation.message);
    error.status = res.status;
    error.reason = data && data.reason;
    error.payload = data;
    error.technicalDetail = presentation.technicalDetail;
    throw error;
  }
  return data;
}

async function preloadAdminSlices() {
  if (!adminState || !window.CognitaAdminState) return [];
  return CognitaAdminState.preload(adminState, (endpoint) => api(endpoint));
}

// Every mutation goes through this coordinator. Keeping the matrix in
// admin-state.js makes omissions reviewable and ensures hidden panels refresh
// just like the visible one.
async function refreshAdminMutation(mutation) {
  if (!adminState || !window.CognitaAdminState) return [];
  const names = CognitaAdminState.invalidateMutation(adminState, mutation);
  await Promise.all(names.map((name) =>
    CognitaAdminState.refresh(adminState, name, (endpoint) => api(endpoint), { force: true })
      .catch(() => null)
  ));
  const cached = (name) => adminState.slices[name].data;
  if (names.includes("projects")) await loadProjects(cached("projects"));
  if (names.includes("connectors")) await loadConnectors(cached("connectors"));
  if (names.includes("oauthGrants") || names.includes("oauthStatus")) {
    await loadOAuth({ status: cached("oauthStatus"), grants: cached("oauthGrants") });
  }
  if (names.includes("authentication")) await loadAuthentication(cached("authentication"));
  if (names.includes("workspaceConnectors")) await loadWorkspaceConnectors(cached("workspaceConnectors"));
  if (names.includes("workspaces")) await loadWorkspaces(cached("workspaces"));
  if (names.includes("workspaceRuntime")) await loadWorkspaceRuntime(cached("workspaceRuntime"));
  if (names.includes("workspaceSettings")) await loadWorkspaceSettings(cached("workspaceSettings"));
  if (names.includes("publicBaseUrl")) await loadPublicBaseUrl(cached("publicBaseUrl"));
  if (names.includes("gpuAcceleration")) renderGpuAcceleration(cached("gpuAcceleration"));
  return names;
}
window.adminMutationCoordinator = refreshAdminMutation;

async function revalidateShellRoute(route) {
  if (!adminState || adminState.session.status !== "ready") return;
  const names = route.top === "projects" ? ["projects"]
    : route.top === "connectors" ? ["connectors", "projects", "oauthStatus", "oauthGrants", "publicBaseUrl"]
      : route.top === "workspaces" ? ["workspaceConnectors", "workspaces", "workspaceRuntime", "workspaceSettings"]
      : route.top === "settings" ? ["gpuAcceleration"] : ["authentication"];
  const results = await Promise.allSettled(names.map((name) =>
    CognitaAdminState.refresh(adminState, name, (endpoint) => api(endpoint))
  ));
  if (results.every((result) => result.status === "rejected")) return;
  const cached = (name) => adminState.slices[name].data;
  if (route.top === "projects") await loadProjects(cached("projects"));
  else if (route.top === "connectors") {
    await Promise.all([
      loadProjects(cached("projects")),
      loadConnectors(cached("connectors")),
      loadPublicBaseUrl(cached("publicBaseUrl")),
      loadOAuth({ status: cached("oauthStatus"), grants: cached("oauthGrants") }),
    ]);
  } else if (route.top === "workspaces") {
    await Promise.all([
      loadWorkspaceConnectors(cached("workspaceConnectors")),
      loadWorkspaces(cached("workspaces")),
      loadWorkspaceRuntime(cached("workspaceRuntime")),
      loadWorkspaceSettings(cached("workspaceSettings")),
    ]);
  } else if (route.top === "settings") {
    renderGpuAcceleration(cached("gpuAcceleration"));
  }
  else await loadAuthentication(cached("authentication"));
}

// ---- theme switcher ----
// Persistence is the cognita_theme cookie; the server reads it and stamps
// data-theme on <html> before the page is sent (no flash). This only keeps the
// <select> in sync with what the server painted and rewrites the cookie + live
// attribute when the admin picks a different theme. "System" is no cookie and
// no data-theme: the page then follows the browser's prefers-color-scheme.
function initThemeSwitch() {
  const sel = $("#theme-select");
  if (!sel) return;
  const painted = document.documentElement.dataset.theme;
  sel.value = painted === "dark" || painted === "light" ? painted : "system";
  sel.addEventListener("change", () => {
    if (sel.value === "system") {
      delete document.documentElement.dataset.theme;
      document.cookie = "cognita_theme=; path=/; max-age=0; samesite=lax";
      return;
    }
    const theme = sel.value === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
    const oneYear = 60 * 60 * 24 * 365;
    document.cookie = `cognita_theme=${theme}; path=/; max-age=${oneYear}; samesite=lax`;
  });
}

async function initSession() {
  let s;
  try { s = await api("/api/session"); } catch { return; }
  if (s && s.auth_required && s.authenticated) {
    $("#session-user").textContent = window.CognitaAdminLocale.t("admin.session.signed_in", { username: s.username });
    $("#logout-btn").style.display = "";
  }
  if (adminState) {
    adminState.session = { status: "ready", data: s, error: null };
  }
  return s;
}

async function initBootstrap() {
  try {
    const bootstrap = await api("/api/bootstrap");
    if (bootstrap && typeof bootstrap.version === "string") {
      state.version = bootstrap.version;
      const version = document.querySelector("#cognita-version");
      if (version) version.textContent = bootstrap.version;
      document.title = t("admin.title", { version: bootstrap.version });
    }
  } catch { /* the server-rendered version remains authoritative */ }
}

function pill(status) {
  const s = String(status || "stopped").toLowerCase();
  const messageId = ({ running: "admin.project_state.running", stopped: "admin.project_state.stopped",
    starting: "admin.project_state.starting", error: "admin.project_state.error" })[s];
  return `<span class="pill ${esc(s)}">${esc(messageId ? window.CognitaAdminLocale.t(messageId) : s)}</span>`;
}

function watcherQueueEnabled(watcherStatus) {
  return Boolean(watcherStatus);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function loadProjects() {
  const body = $("#projects-body");
  body.innerHTML = `<tr><td colspan="6" aria-busy="true">${esc(t("admin.loading"))}</td></tr>`;
  let data;
  try {
    data = await api("/api/projects");
  } catch (err) {
    body.innerHTML = `<tr><td colspan="6">${esc(t("admin.projects.load_failed"))}: ${esc(inlineErrorText(err))}</td></tr>`;
    return;
  }
  if (!data.projects.length) {
    body.innerHTML = `<tr><td colspan="6"><em>${esc(t("admin.projects.empty_state"))}</em></td></tr>`;
    return;
  }
  body.innerHTML = data.projects
    .map(
      (p) => `<tr data-name="${esc(p.name)}">
        <td><strong>${esc(p.name)}</strong>${p.writable ? "" : ' <small class="muted">(read-only)</small>'}</td>
        <td><code>${esc(p.documents_display || p.documents_dir)}</code></td>
        <td class="doc-count muted">…</td>
        <td>${pill(p.worker_status)}</td>
        <td>${p.connected_clients || 0}</td>
        <td class="actions">
          <button class="secondary outline" data-act="copy" data-url="${esc(p.connector_url || p.connector_path)}">Copy connector URL</button>
          <button class="secondary outline" data-act="connections">Manage connections</button>
          <button class="secondary outline" data-act="reindex">Reindex</button>
          <button class="secondary outline" data-act="clear-watcher-queue" disabled>${esc(t("admin.projects.clear_watcher_queue"))}</button>
          <button class="contrast outline" data-act="remove">Remove</button>
        </td>
      </tr>`
    )
    .join("");
  // Load project status for watcher availability; only running projects have index counts.
  for (const p of data.projects) {
    fillDocCount(p.name);
  }
}

async function fillDocCount(name) {
  const cell = document.querySelector(`tr[data-name="${CSS.escape(name)}"] .doc-count`);
  if (!cell) return;
  try {
    const s = await api(`/api/projects/${encodeURIComponent(name)}/status`);
    cell.textContent = s.doc_count == null ? "—" : window.CognitaAdminLocale.number(s.doc_count);
    const queueButton = cell.closest("tr")?.querySelector('[data-act="clear-watcher-queue"]');
    if (queueButton) queueButton.disabled = !watcherQueueEnabled(s.watcher);
    if (s.chunk_count != null) cell.title = window.CognitaAdminLocale.t("admin.projects.chunk_count", {
      count: window.CognitaAdminLocale.number(s.chunk_count),
    });
    // Installer design 7.3: show the last reindex error (for example "The documents folder
    // ... is empty or not mounted. Nothing was removed.") under the folder path.
    const folderCell = cell.parentElement.children[1];
    const previous = folderCell.querySelector(".reindex-error");
    if (previous) previous.remove();
    if (s.reindex_error) {
      const note = document.createElement("small");
      note.className = "reindex-error";
      note.style.display = "block";
      note.style.color = "var(--pico-del-color)";
      note.textContent = `${t("admin.projects.reindex_failed")}: ${t("admin.technical_detail")}: ${s.reindex_error}`;
      folderCell.appendChild(note);
    }
  } catch {
    cell.textContent = "—";
  }
}

const copyTextState = new WeakMap();

async function copyText(text, btn) {
  let state = copyTextState.get(btn);
  if (!state) {
    state = { originalLabel: btn.textContent, timer: null };
    copyTextState.set(btn, state);
  }
  if (state.timer !== null) clearTimeout(state.timer);
  let ok = true;
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = t("admin.action.copied_item");
  } catch {
    ok = false;
    btn.textContent = t("admin.action.copy_failed"); // manual click retries
  }
  state.timer = setTimeout(() => {
    btn.textContent = state.originalLabel;
    copyTextState.delete(btn);
  }, 1500);
  return ok;
}

function when(ts) {
  if (!ts) return window.CognitaAdminLocale.t("admin.status.never");
  const value = typeof ts === "number" ? ts * 1000 : ts;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? window.CognitaAdminLocale.t("admin.status.unavailable") :
    (window.CognitaAdminLocale ? window.CognitaAdminLocale.date(parsed, { dateStyle: "medium", timeStyle: "short" }) : parsed.toLocaleString());
}

// ---- Cognita 9 project, connector, and authorization views ----
async function loadProjectsV9(cached = null, preloaded = false) {
  const body = $("#projects-body");
  body.innerHTML = '<tr><td colspan="6" aria-busy="true">' + esc(t("admin.loading")) + "</td></tr>";
  try {
    const data = preloaded ? cached : (cached || await api("/api/projects"));
    state.projects = Array.isArray(data.projects) ? data.projects : [];
    if (!state.projects.length) {
      body.innerHTML = '<tr><td colspan="6"><em>' + esc(t("admin.projects.empty_state")) + "</em></td></tr>";
      captureConnectorDraft();
      renderConnectorEditorProjects(selectedConnector());
      return data;
    }
    body.innerHTML = state.projects.map((p) =>
      '<tr data-name="' + esc(p.name) + '"' + (state.highlightedProject === p.name ? ' class="project-highlight" tabindex="-1"' : '') + '>' +
      "<td><strong>" + esc(p.name) + "</strong>" +
      (p.enabled ? "" : ' <small class="muted">(' + esc(t("admin.projects.disabled")) + ")</small>") +
      (p.exclude_from_default_permissions ? ' <small class="muted">(' + esc(t("admin.projects.excluded_defaults")) + ")</small>" : "") + "</td>" +
      "<td><code>" + esc(p.documents_display || p.documents_dir) + "</code></td>" +
      '<td class="doc-count muted">…</td><td>' + pill(p.worker_status) + "</td>" +
      "<td>" + (p.connected_clients == null ? "—" : esc(p.connected_clients)) + "</td>" +
      '<td class="actions"><button class="secondary outline" data-act="settings">' + esc(t("admin.action.settings")) + "</button> " +
      '<button class="secondary outline" data-act="connections">' + esc(t("admin.oauth.view_access")) + "</button> " +
      '<button class="secondary outline" data-act="reindex">' + esc(t("admin.action.reindex")) + "</button> " +
      '<button class="contrast outline" data-act="remove">' + esc(t("admin.action.remove")) + "</button></td></tr>"
    ).join("");
    for (const project of state.projects) {
      if (project.worker_status === "running") fillDocCount(project.name);
    }
    if (state.highlightedProject) {
      const row = document.querySelector(`tr[data-name="${CSS.escape(state.highlightedProject)}"]`);
      if (row) { row.focus({ preventScroll: true }); row.scrollIntoView({ behavior: "smooth", block: "center" }); }
      state.highlightedProject = null;
    }
    captureConnectorDraft();
    renderConnectorEditorProjects(selectedConnector());
    return data;
  } catch (err) {
    body.innerHTML = '<tr><td colspan="6">' + esc(t("admin.action.failed")) + ": " + esc(inlineErrorText(err)) + "</td></tr>";
    return null;
  }
}

function connectorMode() {
  const radio = document.querySelector('input[name="project-mode"]:checked');
  return radio ? radio.value : "all";
}

function selectedConnector() {
  return state.connectors.find((item) => String(item.id) === String(state.selectedConnectorId)) || null;
}

const connectorFunctionLabels = Object.freeze({
  settings: "admin.action.settings", clients: "admin.projects.authorized_clients",
  access: "admin.oauth.per_project_access", transfer: "admin.workspaces.transfer",
});

function renderConnectorEntityTabs() {
  const row = $("#connector-entity-tabs");
  if (!row) return;
  const records = state.connectors || [];
  const current = state.selectedConnectorId;
  row.innerHTML = records.map((connector) => {
    const id = String(connector.id);
    const selected = current != null && id === String(current);
    const duplicate = records.filter((item) => item.name === connector.name).length > 1;
    return '<button type="button" role="tab" aria-controls="connector-setup-panel" aria-selected="' +
      (selected ? "true" : "false") + '" tabindex="' + (selected ? "0" : "-1") +
      '" data-connector-id="' + esc(id) + '" aria-label="' + esc(connector.name) +
      '"' + (duplicate ? ' aria-description="' + esc(t("admin.connectors.duplicate_description")) + '"' : "") + '>' +
      esc(connector.name) + (connector.enabled ? "" : ' <small class="muted">(' + esc(t("admin.projects.disabled")) + ")</small>") + "</button>";
  }).join("") +
    '<button type="button" role="tab" aria-controls="connector-setup-panel" aria-selected="' +
    (current == null ? "true" : "false") + '" tabindex="' + (current == null ? "0" : "-1") +
    '" data-connector-add="true" data-route="#connectors/add">' + esc(t("admin.action.add_connector")) + "</button>";
  row.querySelectorAll("[role=tab]").forEach((button) => {
    button.addEventListener("click", () => {
      captureConnectorCreateDraft();
      if (button.dataset.connectorAdd) navigateShell(connectorRoute(null, "add"));
      else navigateShell(connectorRoute(button.dataset.connectorId, state.connectorFunctions.get(button.dataset.connectorId) || "settings"));
    });
    button.addEventListener("keydown", (event) => moveTabFocus(event, row));
  });
}

function renderConnectorFunctionTabs(route) {
  const row = $("#connector-tabs");
  if (!row) return;
  const connector = selectedConnector();
  if (!connector || route.sub === "add") { row.hidden = true; row.innerHTML = ""; return; }
  row.hidden = false;
  state.connectorFunctions.set(String(connector.id), route.sub);
  row.innerHTML = Object.entries(connectorFunctionLabels).map(([sub, label]) => {
    const selected = route.sub === sub;
    return '<button type="button" role="tab" id="connector-tab-' + sub + '" aria-controls="connector-' + sub + '-panel" aria-selected="' +
      (selected ? "true" : "false") + '" tabindex="' + (selected ? "0" : "-1") + '" data-function="' + sub + '">' + esc(t(label)) + '</button>';
  }).join("");
  row.querySelectorAll("[role=tab]").forEach((button) => {
    button.dataset.route = CognitaAdminState.routeFragment(connectorRoute(connector.id, button.dataset.function));
    button.addEventListener("click", () => navigateShell(connectorRoute(connector.id, button.dataset.function)));
    button.addEventListener("keydown", (event) => moveTabFocus(event, row));
  });
}

function moveTabFocus(event, row) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const tabs = Array.from(row.querySelectorAll("[role=tab]"));
  const index = tabs.indexOf(event.currentTarget);
  const delta = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + delta + tabs.length) % tabs.length;
  tabs[next]?.focus();
}

function renderConnectorCreate() {
  state.editingConnectorId = null;
  const editor = $("#connector-editor");
  const settings = $("#connector-settings-details");
  const publicUrl = $("#public-url-section");
  if (publicUrl) publicUrl.hidden = true;
  if (settings) settings.hidden = true;
  if (editor) editor.hidden = false;
  const title = $("#connector-editor-title");
  if (title) title.textContent = t("admin.connectors.add");
  const submit = $("#connector-submit");
  if (submit) submit.textContent = t("admin.connectors.add");
  const cancel = $("#connector-cancel");
  if (cancel) cancel.hidden = false;
  const draft = state.connectorCreateDraft;
  if (draft) {
    $("#connector-name").value = draft.name || "";
    $("#connector-enabled").checked = draft.enabled !== false;
    $("#connector-workspace-enabled").checked = Boolean(draft.workspace_enabled);
    $("#connector-transfer-default").value = draft.default_workspace_transfer || "allow";
  } else {
    $("#connector-form").reset();
    $("#connector-enabled").checked = true;
    // 13.0.1: Workspace tools and transfer are on by default.
    $("#connector-workspace-enabled").checked = true;
    $("#connector-transfer-default").value = "allow";
  }
  $("#connector-high-trust-confirm").checked = true;
}

function captureConnectorCreateDraft() {
  if (state.connectorView !== "add") return;
  const name = $("#connector-name");
  if (!name) return;
  state.connectorCreateDraft = {
    name: name.value,
    enabled: $("#connector-enabled").checked,
    workspace_enabled: $("#connector-workspace-enabled").checked,
    default_workspace_transfer: $("#connector-transfer-default").value,
  };
}

function renderConnectorEditor(connector) {
  const editor = $("#connector-editor");
  if (!editor || !connector) return;
  editor.hidden = false;
  state.editingConnectorId = connector.id;
  const title = $("#connector-editor-title");
  if (title) title.textContent = t("admin.connectors.edit_title", { name: connector.name });
  $("#connector-submit").textContent = t("admin.connectors.save");
  $("#connector-cancel").hidden = false;
  const draft = state.connectorSettingsDrafts.get(String(connector.id));
  $("#connector-name").value = draft ? draft.name : connector.name;
  $("#connector-enabled").checked = draft ? draft.enabled : Boolean(connector.enabled);
  $("#connector-workspace-enabled").checked = draft ? draft.workspace_enabled : Boolean(connector.workspace_enabled);
  $("#connector-transfer-default").value = draft
    ? draft.default_workspace_transfer : (connector.default_workspace_transfer || "allow");
  $("#connector-high-trust-confirm").checked = false;
}

function captureConnectorSettingsDraft() {
  const connector = selectedConnector();
  if (!connector || String(state.editingConnectorId) !== String(connector.id) || state.connectorView === "add") return;
  const name = $("#connector-name");
  if (!name) return;
  state.connectorSettingsDrafts.set(String(connector.id), {
    name: name.value,
    enabled: $("#connector-enabled").checked,
    workspace_enabled: $("#connector-workspace-enabled").checked,
    default_workspace_transfer: $("#connector-transfer-default").value,
  });
}

function renderConnectorSettings(connector) {
  const section = $("#connector-settings-details");
  if (!section || !connector) return;
  const publicUrl = $("#public-url-section");
  if (publicUrl) publicUrl.hidden = false;
  section.hidden = false;
  $("#connector-settings-heading").innerHTML = esc(t("admin.connectors.settings_for", { name: connector.name })) +
    ' <span class="actions"><button type="button" class="secondary outline" data-connector-edit="' + esc(connector.id) + '">' + esc(t("admin.connectors.edit")) + '</button> <button type="button" class="contrast outline" data-connector-delete="' + esc(connector.id) + '">' + esc(t("admin.connectors.delete")) + '</button></span>';
  const workspaceStatus = connector.workspace_reason === "host_workspace_disabled"
    ? ` (${t("admin.connectors.not_effective_host")})`
    : (connector.workspace_effective ? ` (${t("admin.connectors.workspace_effective")})` : "");
  $("#connector-settings-summary").textContent = (connector.enabled ? t("admin.connectors.enabled") : t("admin.projects.disabled")) +
    " · Workspace " + t(connector.workspace_requested ? "admin.connectors.workspace_requested" : "admin.connectors.workspace_off") + workspaceStatus +
    " · " + t("admin.connectors.contract_version", { version: connector.contract_version || "—" });
  const stable = connector.stable_url || connector.url || connector.path || "";
  const current = connector.current_url || "";
  $("#connector-mcp-urls").innerHTML =
    '<p><strong>' + esc(t("admin.connectors.stable_url.title")) + '</strong><br><small class="muted">' + esc(t("admin.connectors.stable_url.help")) + '</small></p>' +
    '<div class="connector-url-row"><label>' + esc(t("admin.connectors.stable_url.title")) + '<input class="connector-url" readonly value="' + esc(stable) + '"></label><button type="button" class="secondary outline" data-connector-copy-url="stable" data-url="' + esc(stable) + '">' + esc(t("admin.connectors.copy_url")) + '</button></div>' +
    '<p><strong>' + esc(t("admin.connectors.current_url.title")) + '</strong><br><small class="muted">' + esc(t("admin.connectors.current_url.help")) + '</small></p>' +
    '<div class="connector-url-row"><label>' + esc(t("admin.connectors.current_url.title")) + '<input class="connector-url" readonly value="' + esc(current) + '"></label><button type="button" class="secondary outline" data-connector-copy-url="current" data-url="' + esc(current) + '">' + esc(t("admin.connectors.copy_url")) + '</button></div>';
  $("#connector-settings-credentials").innerHTML = '<strong>' + esc(t("admin.connectors.private_keys")) + '</strong><div class="connector-credentials-list"></div>';
  renderCredentialList("combined", connector.id, $("#connector-settings-credentials .connector-credentials-list"));
}

function connectorDraft(connector) {
  if (!connector) return null;
  const key = String(connector.id);
  if (!state.connectorDrafts.has(key)) {
    state.connectorDrafts.set(key, {
      access: {
        project_mode: connector.project_mode || "all",
        default_access: connector.default_access || "write",
        project_access: { ...(connector.project_access || {}) },
      },
      transfer: {
        default_workspace_transfer: connector.default_workspace_transfer || "allow",
        project_transfer: { ...(connector.project_transfer || {}) },
        confirm_high_trust: false,
      },
    });
  }
  return state.connectorDrafts.get(key);
}

function captureConnectorDraft() {
  const connector = selectedConnector();
  if (!connector || !$("#connector-project-access")?.querySelector("[data-project-access]")) return;
  const draft = connectorDraft(connector);
  const mode = connectorMode();
  const projectAccess = {};
  document.querySelectorAll("#connector-project-access select[data-project-access]").forEach((select) => {
    if (["read", "write"].includes(select.value)) {
      projectAccess[select.dataset.projectAccess] = select.value;
    }
  });
  const projectTransfer = {};
  document.querySelectorAll("#connector-project-transfer select[data-project-transfer]").forEach((select) => {
    if (select.value !== "inherit") projectTransfer[select.dataset.projectTransfer] = select.value;
  });
  draft.access = {
    project_mode: mode,
    default_access: mode === "all" && $("#connector-default-readonly").checked ? "read" :
      (mode === "all" ? "write" : null),
    project_access: projectAccess,
  };
  draft.transfer = {
    default_workspace_transfer: $("#connector-policy-transfer-default").value,
    project_transfer: projectTransfer,
    confirm_high_trust: $("#connector-policy-high-trust-confirm").checked,
  };
}

function syncConnectorSelectors() {
  const connectors = state.connectors || [];
  const previousSelected = state.selectedConnectorId;
  const usable = connectors.find((item) => item.enabled) || connectors[0] || null;
  if (!state.selectedConnectorId || !connectors.some((item) => String(item.id) === String(state.selectedConnectorId))) {
    state.selectedConnectorId = usable && usable.id || null;
  }
  document.querySelectorAll("[data-no-connectors]").forEach((empty) => { empty.hidden = connectors.length > 0; });
  ["#connector-access-view", "#connector-transfer-view"].forEach((selector) => {
    const content = $(selector); if (content) content.hidden = !connectors.length;
  });
  const connector = selectedConnector();
  const hasDraftControls = Boolean($("#connector-project-access")?.querySelector("[data-project-access]") || $("#connector-project-transfer")?.querySelector("[data-project-transfer]"));
  const hasStoredDraft = connector && state.connectorDrafts.has(String(connector.id));
  if (connector && (!hasDraftControls || !hasStoredDraft ||
      String(previousSelected) !== String(state.selectedConnectorId))) {
    renderConnectorEditorProjects(connector);
  } else if (!connector) {
    const transferStatus = $("#connector-transfer-status");
    if (transferStatus) transferStatus.textContent = t("admin.connectors.transfer_unavailable");
    ["#connector-access-view", "#connector-transfer-view"].forEach((selector) => { const view = $(selector); if (view) view.hidden = true; });
  }
}

function selectConnector(id) {
  const connector = state.connectors.find((item) => String(item.id) === String(id));
  if (!connector) return;
  captureConnectorDraft();
  state.selectedConnectorId = connector.id;
  renderConnectorEditorProjects(connector);
}

function renderConnectorEditorProjects(connector) {
  const container = $("#connector-project-access");
  const transferContainer = $("#connector-project-transfer");
  if (!container) return;
  const transferStatus = $("#connector-transfer-status");
  if (transferStatus) {
    transferStatus.innerHTML = connector && connector.workspace_enabled
      ? esc(t("admin.connectors.workspace_capability_enabled"))
      : esc(t("admin.connectors.workspace_capability_disabled")) + ' <button type="button" class="secondary outline" data-go-connector-setup>' + esc(t("admin.connectors.add_edit")) + '</button>';
  }
  const transferView = $("#connector-transfer-view");
  if (transferView && connector) transferView.hidden = !connector.workspace_enabled;
  const draft = connectorDraft(connector);
  const accessDraft = draft && draft.access;
  const transferDraft = draft && draft.transfer;
  const mode = accessDraft ? accessDraft.project_mode : connectorMode();
  const configured = accessDraft ? accessDraft.project_access : {};
  document.querySelectorAll('input[name="project-mode"]').forEach((radio) => {
    radio.checked = radio.value === mode;
  });
  if (accessDraft) $("#connector-default-readonly").checked = accessDraft.default_access === "read";
  if (transferDraft) {
    $("#connector-policy-transfer-default").value = transferDraft.default_workspace_transfer;
    $("#connector-policy-high-trust-confirm").checked = transferDraft.confirm_high_trust;
  }
  if (!state.projects.length) {
    container.innerHTML = '<span class="muted">' + esc(t("admin.connectors.empty_access")) + '</span>';
    if (transferContainer) transferContainer.innerHTML = '<span class="muted">' + esc(t("admin.connectors.empty_transfer")) + '</span>';
  } else {
    container.innerHTML = state.projects.map((project) => {
      const configuredValue =
        (Object.prototype.hasOwnProperty.call(configured, project.name)
          ? configured[project.name] : (mode === "all" ? "inherit" : "none"));
      const value = mode === "all"
        ? (["inherit", "read", "write"].includes(configuredValue) ? configuredValue : "inherit")
        : (["none", "read", "write"].includes(configuredValue) ? configuredValue : "none");
      const options = mode === "all"
          ? '<option value="inherit">' + esc(project.exclude_from_default_permissions ? t("admin.connectors.option.excluded_default") : t("admin.connectors.option.inherit_default")) + '</option><option value="read">' + esc(t("admin.oauth.read_only")) + '</option><option value="write">' + esc(t("admin.oauth.read_write")) + '</option>'
        : '<option value="none">' + esc(t("admin.connectors.option.not_selected")) + '</option><option value="read">' + esc(t("admin.oauth.read_only")) + '</option><option value="write">' + esc(t("admin.oauth.read_write")) + '</option>';
      return '<label for="connector-access-' + encodeURIComponent(project.name) + '">' +
        esc(project.name) + (project.enabled ? "" : " " + esc(t("admin.connectors.option.disabled_project"))) + "</label>" +
        '<select id="connector-access-' + encodeURIComponent(project.name) +
        '" data-project-access="' + esc(project.name) + '"' +
        (project.enabled ? "" : " disabled") + ">" +
        options.replace('value="' + value + '"', 'value="' + value + '" selected') + "</select>";
    }).join("");
    if (transferContainer) {
      const configuredTransfer = transferDraft ? transferDraft.project_transfer : {};
      transferContainer.innerHTML = state.projects.map((project) => {
        const selected = ["inherit", "allow", "deny"].includes(configuredTransfer[project.name]) ? configuredTransfer[project.name] : "inherit";
        return '<label for="connector-transfer-' + encodeURIComponent(project.name) + '">' + esc(project.name) + '</label>' +
          '<select id="connector-transfer-' + encodeURIComponent(project.name) + '" data-project-transfer="' + esc(project.name) + '">' +
          '<option value="inherit"' + (selected === "inherit" ? " selected" : "") + '>' + esc(t("admin.connectors.option.inherit")) + '</option><option value="allow"' + (selected === "allow" ? " selected" : "") + '>' + esc(t("admin.connectors.option.allow")) + '</option><option value="deny"' + (selected === "deny" ? " selected" : "") + '>' + esc(t("admin.connectors.option.deny")) + '</option></select>';
      }).join("");
    }
  }
  const allMode = mode === "all";
  $("#connector-default-row").hidden = !allMode;
  $("#connector-project-help").textContent = allMode
    ? t("admin.connectors.access_help_all")
    : t("admin.connectors.access_help_selected");
}

function connectorSummary(connector) {
  const accesses = Object.entries(connector.project_access || {});
  if (connector.project_mode === "selected") {
    const selected = accesses.map(([name, access]) =>
      name + ": " + (access === "read" ? "Read-only" : "Read/write"));
    return selected.length ? "Selected projects\n" + selected.join("\n") :
      "Selected projects\nNo projects selected";
  }
  const defaultText = connector.default_access === "read" ? "Read-only" : "Read/write";
  const overrides = accesses.map(([name, access]) =>
    name + ": " + (access === "read" ? "Read-only" : "Read/write"));
  return "All enabled projects\nDefault: " + defaultText +
    (overrides.length ? "\nOverrides:\n" + overrides.join("\n") : "") +
    "\nIncludes future enabled projects unless they are excluded from defaults.";
}

function renderConnectors() {
  const body = $("#connectors-body");
  if (body) body.innerHTML = state.connectors.length
    ? '<p class="muted">' + esc(t("admin.connectors.select_prompt")) + '</p>'
    : '<p><em>' + esc(t("admin.connectors.empty")) + '</em></p>';
  renderConnectorEntityTabs();
}

async function deleteConnector(connector) {
  if (!connector) return;
  const result = await uiConfirm({
    title: t("admin.connectors.delete.confirm.title"),
    body: t("admin.connectors.delete.confirm.body", { name: connector.name }),
    confirmLabel: t("admin.connectors.delete"), danger: true,
  });
  if (!result.ok) return;
  const index = state.connectors.findIndex((item) => String(item.id) === String(connector.id));
  try {
    await api("/api/connectors/" + encodeURIComponent(connector.id) +
      "?expected_revision=" + state.connectorsRevision, { method: "DELETE" });
    state.connectorDrafts.delete(String(connector.id));
    state.connectorSettingsDrafts.delete(String(connector.id));
    const remaining = state.connectors.filter((item) => String(item.id) !== String(connector.id));
    const neighbor = remaining[index] || remaining[index - 1] || null;
    state.selectedConnectorId = neighbor ? neighbor.id : null;
    state.lastExistingConnectorId = state.selectedConnectorId;
    const nextRoute = neighbor ? connectorRoute(neighbor.id, "settings") : connectorRoute(null, "add");
    window.history.replaceState(null, "", CognitaAdminState.routeFragment(nextRoute));
    await refreshAdminMutation("connector:delete");
    activateShellRoute(nextRoute, true);
    const nextTab = document.querySelector(nextRoute.connectorId == null
      ? '[data-connector-add="true"]'
      : '[data-connector-id="' + CSS.escape(String(nextRoute.connectorId)) + '"]');
    if (nextTab) nextTab.focus();
  } catch (err) {
    await handleConnectorError(err);
  }
}

async function renderCredentialList(surfaceKind, surfaceId, container) {
  try {
    const requestedSurfaceId = String(surfaceId);
    const prefix = surfaceKind === "workspace" ? "/api/workspace-connectors/" : "/api/connectors/";
    const data = await api(prefix + encodeURIComponent(surfaceId) + "/credentials");
    if (surfaceKind === "combined" && String(state.selectedConnectorId) !== requestedSurfaceId) return;
    const credentials = Array.isArray(data.credentials) ? data.credentials : [];
    const { panel, list } = credentialPanelOf(container);
    panel.dataset.credentialRevision = Number.isInteger(data.revision) ? String(data.revision) : "0";
    container = list;
    container.innerHTML = '<div class="credential-toolbar"><button type="button" class="secondary outline" data-credential-add="' + esc(surfaceKind) + '" data-surface-id="' + esc(surfaceId) + '">' + esc(t("admin.credential.action.add")) + '</button></div>' +
      (credentials.length ? credentials.map((item) => {
        const target = workspaceTargetForCredential(item);
        const status = item.status || (item.revoked ? "revoked" : "active");
        const statusLabel = ({ active: t("admin.option.active"), revoked: t("admin.option.revoked") })[status] || status;
        const id = esc(item.credential_id || "");
        const surface = ' data-surface-kind="' + esc(surfaceKind) + '" data-surface-id="' + esc(surfaceId) + '" data-credential-id="' + id + '"';
        return '<div class="credential-row" data-credential-id="' + id + '" data-workspace-id="' + esc(target.id || "") + '" data-workspace-state="' + esc(target.state || "") + '" data-workspace-owner="' + esc(target.ownerStatus || "") + '"><strong>' + esc(item.label || item.name || t("admin.credential.unnamed")) + '</strong> <small class="muted">' + esc(statusLabel) + '</small><br><small class="muted">' + esc(t("admin.credential.field.uuid")) + ': <code>' + esc(item.credential_id || item.key_id || "—") + '</code></small>' +
          '<span class="credential-actions">' + (surfaceKind === "workspace" ? '<button type="button" class="secondary outline" data-credential-action="setup"' + surface + '>' + esc(t("admin.credential.action.setup")) + '</button> ' : '') + '<button type="button" class="secondary outline" data-credential-action="reveal"' + surface + '>' + esc(t("admin.credential.action.reveal")) + '</button> <button type="button" class="secondary outline" data-credential-action="rotate"' + surface + '>' + esc(t("admin.credential.action.rotate")) + '</button> <button type="button" class="secondary outline" data-credential-action="revoke"' + surface + '>' + esc(t("admin.credential.action.revoke")) + '</button> <button type="button" class="contrast outline" data-credential-action="delete"' + surface + '>' + esc(t("admin.credential.action.delete")) + '</button></span><div class="connection-instructions" hidden></div></div>';
      }).join("") : '<p class="muted">' + esc(t("admin.credential.empty")) + '</p>');
  } catch (err) {
    container.innerHTML = '<p role="alert">' + esc(t("admin.credential.policy_unavailable")) + ': ' + esc(inlineErrorText(err)) + '</p>';
  }
}

function showCredentialSecret(payload, title) {
  // Secrets remain transient and are never added to adminState, localStorage,
  // or URL fragments.
  const secret = payload && payload.secret;
  if (!secret) return uiNotice({ title: title || t("admin.action.completed"), body: t("admin.credential.secret.missing") });
  const dialog = $("#credential-secret-dialog");
  $("#credential-secret-title").textContent = title || t("admin.credential.secret.title");
  $("#credential-secret-value").textContent = secret;
  dialog.showModal();
  return Promise.resolve({ ok: true });
}

$("#copy-credential-secret").addEventListener("click", async (event) => {
  await copyText($("#credential-secret-value").textContent, event.currentTarget);
});
$("#credential-secret-done").addEventListener("click", () => $("#credential-secret-dialog").close());
$("#credential-secret-dialog").addEventListener("close", () => { $("#credential-secret-value").textContent = ""; });

async function loadWorkspaceConnectors(cached = null, preloaded = false) {
  try {
    const data = preloaded ? cached : (cached || await api("/api/workspace-connectors"));
    state.workspaceConnectors = Array.isArray(data && data.workspace_connectors) ? data.workspace_connectors : [];
    state.workspaceConnectorsRevision = Number.isInteger(data && data.revision) ? data.revision : 0;
    const label = $("#workspace-connectors-revision");
    if (label) label.textContent = t("admin.workspace_connector.policy_revision", { revision: state.workspaceConnectorsRevision });
    renderWorkspaceConnectors(data || {});
    return data;
  } catch (err) {
    const label = $("#workspace-connectors-revision");
    if (label) label.textContent = t("admin.status.unavailable");
    const body = $("#workspace-connectors-body");
    if (body) body.innerHTML = '<p role="alert">' + esc(t("admin.workspace_connector.policy_unavailable")) + ': ' + esc(inlineErrorText(err)) + '</p>';
    return null;
  }
}

async function loadWorkspaces(cached = null, preloaded = false) {
  try {
    const query = new URLSearchParams({
      sort: $("#workspace-sort")?.value || "credential",
      direction: state.workspaceSortDirection || "asc",
      search: $("#workspace-search")?.value || "",
      state: $("#workspace-state")?.value || "",
    });
    ["pinned", "expired", "over_warning"].forEach((key) => {
      const id = key === "over_warning" ? "#workspace-warning" : `#workspace-${key}`;
      const value = $(id)?.value;
      if (value !== "" && value !== undefined) query.set(key, value);
    });
    const owner = $("#workspace-owner")?.value;
    if (owner) query.set("owner_status", owner);
    if (state.workspaceCursor) query.set("cursor", state.workspaceCursor);
    const data = preloaded ? cached : (cached || await api("/api/workspaces?" + query.toString()));
    state.workspaces = Array.isArray(data && data.workspaces) ? data.workspaces : [];
    state.workspacesRevision = Number.isInteger(data && data.revision) ? data.revision : 0;
    state.workspaceNextCursor = data && data.next_cursor || null;
    renderWorkspaces(data || {});
    return data;
  } catch (err) {
    const body = $("#workspaces-body");
    if (body) body.innerHTML = '<p role="alert">' + esc(t("admin.workspace.lifecycle_unavailable")) + ': ' + esc(inlineErrorText(err)) + '</p>';
    return null;
  }
}

async function loadWorkspaceRuntime(cached = null, preloaded = false) {
  try {
    const data = preloaded ? cached : (cached || await api("/api/workspaces/health"));
    state.workspaceRuntime = data && (data.runtime || data) || {};
    renderWorkspaceRuntime(data || {});
    return data;
  } catch (err) {
    const label = $("#workspaces-status");
    if (label) label.textContent = t("admin.workspace.runtime_unavailable");
    // Do not leave a successful capacity sample on screen after a refresh
    // fails. Clear numeric values and mark the replacement state as stale.
    state.workspaceRuntime = {
      status: "unavailable",
      runtime_probe_status: "unavailable",
      storage: {
        measurement_status: "stale",
        measurement_reason: "Runtime measurement refresh failed",
        measurement_source: "Admin runtime endpoint",
        measured_at: null,
        filesystem_capacity_bytes: null,
        filesystem_free_bytes: null,
        admissible_free_bytes: null,
        workspace_allocated_bytes: null,
        workspace_apparent_bytes: null,
      },
    };
    renderWorkspaceRuntime(state.workspaceRuntime);
    return null;
  }
}

async function loadWorkspaceSettings(cached = null, preloaded = false) {
  try {
    const data = preloaded ? cached : (cached || await api("/api/workspace-settings"));
    state.workspaceSettings = data && data.settings || {};
    renderWorkspaceSettings(data || {});
    return data;
  } catch (err) {
    const label = $("#workspace-settings-status");
    if (label) label.textContent = t("admin.status.unavailable");
    return null;
  }
}

async function loadGpuAcceleration(cached = null, preloaded = false) {
  try {
    const data = preloaded ? cached : (cached || await api("/api/settings/gpu-acceleration"));
    state.gpuAcceleration = data || {};
    renderGpuAcceleration(data || {});
    return data;
  } catch (err) {
    const label = $("#gpu-acceleration-status");
    if (label) label.textContent = t("admin.gpu.unavailable");
    const body = $("#gpu-effective-state");
    if (body) body.innerHTML = '<p role="alert">' + esc(t("admin.gpu.status_unavailable")) + ': ' + esc(inlineErrorText(err)) + '</p>';
    return null;
  }
}

// Reason tokens a person has to act on, in plain words. Every other token is shown as
// the bounded enum value the API sends (DESIGN-12.4.0 §5). 15.0: driver_too_old is the
// NVIDIA driver being older than the CUDA 13 floor.
// Every reason the status can report (acceleration.FALLBACK_REASONS), in words. An unknown
// token still shows as itself rather than disappearing.
const GPU_REASON_IDS = Object.freeze({
  driver_too_old: "admin.gpu.reason.driver_too_old", profile_cpu: "admin.gpu.reason.profile_cpu",
  device_nodes_missing: "admin.gpu.reason.device_nodes_missing", device_permission_denied: "admin.gpu.reason.device_permission_denied",
  runtime_missing: "admin.gpu.reason.runtime_missing", runtime_integrity_failed: "admin.gpu.reason.runtime_integrity_failed",
  provider_unavailable: "admin.gpu.reason.provider_unavailable", provider_cpu_fallback: "admin.gpu.reason.provider_cpu_fallback",
  canary_failed: "admin.gpu.reason.canary_failed", model_unqualified: "admin.gpu.reason.model_unqualified",
  not_selected: "admin.gpu.reason.not_selected", configured_card_missing: "admin.gpu.reason.configured_card_missing",
  stable_uuid_missing: "admin.gpu.reason.stable_uuid_missing", insufficient_vram: "admin.gpu.reason.insufficient_vram",
  busy: "admin.gpu.reason.busy", telemetry_unavailable: "admin.gpu.reason.telemetry_unavailable",
  cooldown: "admin.gpu.reason.cooldown", quarantined: "admin.gpu.reason.quarantined",
  configuration_invalid: "admin.gpu.reason.configuration_invalid", restart_required: "admin.gpu.reason.restart_required",
  verification_timeout: "admin.gpu.reason.verification_timeout", worker_cleanup_failed: "admin.gpu.reason.worker_cleanup_failed",
  knowledge_gpu_off: "admin.gpu.reason.knowledge_gpu_off",
});
const GPU_VERIFY_STATE_IDS = Object.freeze({
  passed: "admin.gpu.verify_state.passed", fallback: "admin.gpu.verify_state.fallback",
  failed: "admin.gpu.verify_state.failed", timeout: "admin.gpu.verify_state.timeout",
});

function gpuReasonText(reason) {
  const id = GPU_REASON_IDS[reason];
  return id ? t(id) : reason;
}

// The empty-cards sentence, from the status's own profile: the vendor label comes from the
// server's profile table (deployment.vendor_label), and a CPU profile exposes no cards at all.
function gpuNoCardsText(deployment) {
  const profile = (deployment && deployment.profile) || "cpu";
  if (profile === "cpu") return t("admin.gpu.no_cards_cpu");
  const vendor = (deployment && deployment.vendor_label) || "GPU";
  return t("admin.gpu.no_cards_vendor", { vendor });
}

function renderGpuAcceleration(data) {
  if (!data) return;
  state.gpuAcceleration = data;
  const deployment = data.deployment || {};
  const configured = data.configured || {};
  const loaded = data.loaded || {};
  const effective = data.effective || {};
  const restart = data.restart || {};
  const label = $("#gpu-acceleration-status");
  if (label) label.textContent = t("admin.gpu.revision_status", { profile: deployment.profile || "cpu", configured: configured.revision ?? "—", loaded: loaded.revision ?? "—" });
  const stateCard = $("#gpu-effective-state");
  // 15.0.3: before Verify has run in this process the service is already using the card
  // (indexing checks each card itself when it starts it), so say so, and say it is unchecked.
  const unverified = ((data.verification || {}).state || "not_run") === "not_run";
  const onCard = (gpu) => gpu ? (unverified ? t("admin.gpu.unverified") : t("admin.gpu.active")) : t("admin.gpu.cpu_fallback");
  if (stateCard) stateCard.innerHTML = `<h3>${esc(t("admin.gpu.effective_state"))}</h3><p><strong>${esc(t("admin.gpu.profile"))}:</strong> ${esc(deployment.profile || "cpu")}</p><p><strong>${esc(t("admin.gpu.knowledge"))}:</strong> ${esc(onCard(effective.knowledge_gpu))} · <strong>${esc(t("admin.gpu.ocr"))}:</strong> ${esc(onCard(effective.ocr_device === "gpu"))}</p>${restart.required ? `<p class="auth-warning" role="alert"><strong>${esc(t("admin.gpu.restart_required"))}</strong> ${esc(t("admin.gpu.restart_not_loaded"))}</p>` : ""}<p class="muted">${esc((effective.fallback_reasons || []).map(gpuReasonText).join(" ") || t("admin.gpu.no_fallback_reason"))}</p>`;
  const cards = $("#gpu-detected-cards");
  if (cards) {
    const rows = Array.isArray(data.cards) ? data.cards : [];
    const cardReady = unverified ? t("admin.gpu.ready_unverified") : t("admin.gpu.ready");
    cards.innerHTML = `<h3>${esc(t("admin.gpu.detected_cards"))}</h3>${rows.length ? `<ul>${rows.map((card) => `<li><strong>${esc(card.name || card.pci_address)}</strong> <span class="muted">${esc(card.pci_address)}${card.gpu_uuid ? ` · <code>${esc(card.gpu_uuid)}</code>` : ""} · ${formatBytes(Number(card.vram_free_bytes) || 0)} ${esc(t("admin.gpu.free"))} / ${formatBytes(Number(card.vram_total_bytes) || 0)} · ${esc(t("admin.gpu.knowledge"))} ${card.knowledge_usable ? cardReady : esc(t("admin.gpu.fallback"))} · ${esc(t("admin.gpu.ocr"))} ${card.ocr_usable ? cardReady : esc(t("admin.gpu.fallback"))}${card.reasons?.length ? " · " + esc(card.reasons.map(gpuReasonText).join(" ")) : ""}${card.indexing_state === "quarantined" && card.indexing_reason ? " (" + esc(card.indexing_reason) + ")" : ""}</span></li>`).join("")}</ul>` : `<p class="muted">${esc(gpuNoCardsText(deployment))}</p>`}`;
  }
  const knowledge = configured.knowledge || {};
  const ocr = configured.ocr || {};
  const enabled = $("#gpu-knowledge-enabled"); if (enabled) enabled.checked = Boolean(knowledge.gpu_enabled);
  const cardsMode = $("#gpu-knowledge-cards"); if (cardsMode) cardsMode.value = knowledge.gpu_device_ids?.length ? "specific" : "all";
  const cardIds = $("#gpu-knowledge-card-ids"); if (cardIds) cardIds.value = (knowledge.gpu_device_ids || []).join("\n");
  const ocrDevice = $("#gpu-ocr-device"); if (ocrDevice) ocrDevice.value = ocr.device || "cpu";
  const ocrIds = $("#gpu-ocr-card-ids"); if (ocrIds) ocrIds.value = (ocr.gpu_device_ids || []).join("\n");
  const specific = $("#gpu-knowledge-card-ids-row"); if (specific) specific.hidden = cardsMode?.value !== "specific";
  const ocrSpecific = $("#gpu-ocr-card-ids-row"); if (ocrSpecific) ocrSpecific.hidden = ocrDevice?.value !== "gpu";
  const help = $("#gpu-acceleration-help"); if (help) help.textContent = restart.required ? t("admin.gpu.saved_restart") : t("admin.gpu.loaded_policy");
}

function workspaceValue(row, ...keys) {
  for (const key of keys) if (row && row[key] !== undefined && row[key] !== null) return row[key];
  return "—";
}

function workspaceMeasuredValue(row, currentKey, legacyKey) {
  if (row && Object.prototype.hasOwnProperty.call(row, currentKey)) return row[currentKey];
  return row && row[legacyKey];
}

function isVerifiedReclaimEstimate(status, value) {
  return status === "verified" && typeof value === "number" &&
    Number.isFinite(value) && value >= 0;
}

function resetWorkspacePagination() {
  state.workspaceCursor = null;
  state.workspaceCursorStack = [];
}

async function fetchCredentialWorkspaceTarget(credentialId) {
  if (!credentialId) return { workspace: null, revision: null };
  const query = new URLSearchParams({
    search: String(credentialId),
    sort: "credential",
    direction: "asc",
    limit: "2",
  });
  const result = await api("/api/workspaces?" + query.toString());
  const rows = Array.isArray(result && result.workspaces) ? result.workspaces : [];
  const matches = rows.filter((row) => String(row.credential_id || row.key_id || "") === String(credentialId));
  if (matches.length > 1) throw new Error(t("admin.credential.multiple_workspaces"));
  return { workspace: matches[0] || null, revision: result && result.revision };
}

function workspaceTargetForCredential(item) {
  const credentialId = item && (item.credential_id || item.key_id);
  const live = (Array.isArray(state.workspaces) ? state.workspaces : []).find((row) =>
    credentialId && String(row.credential_id || row.key_id) === String(credentialId)
  );
  const id = (live && (live.workspace_id || live.id)) || (item && item.workspace_id);
  return {
    id: id || null,
    state: (live && (live.state || live.status)) || (item && item.workspace_state) || null,
    desiredState: live && live.desired_state,
    ownerStatus: live && live.owner_status,
  };
}

function renderWorkspaceRuntime(data) {
  const runtime = data && (data.runtime || data) || {};
  const storage = data && data.storage || runtime.storage || {};
  const label = $("#workspaces-status");
  if (label) label.textContent = workspaceValue(runtime, "status", "runtime");
  const cards = $("#workspace-runtime-cards");
  if (!cards) return;
  const formatCount = (value) => value !== "—" && Number.isFinite(Number(value))
    ? window.CognitaAdminLocale.number(Number(value)) : String(value);
  cards.innerHTML = [
    [t("admin.workspace.metric.runtime"), workspaceValue(runtime, "runtime")],
    [t("admin.workspace.metric.probe"), workspaceValue(runtime, "runtime_probe_status", "status")],
    [t("admin.workspace.metric.running"), formatCount(workspaceValue(runtime, "running_count")) + " / " + formatCount(workspaceValue(runtime, "running_capacity", "capacity"))],
    [t("admin.workspace.metric.host_root"), workspaceValue(storage, "host_root")],
    [t("admin.workspace.metric.container_root"), workspaceValue(storage, "container_root")],
    [t("admin.workspace.metric.filesystem"), formatMaybeBytes(workspaceValue(storage, "filesystem_capacity_bytes"))],
    [t("admin.workspace.metric.free_reserve"), formatMaybeBytes(workspaceValue(storage, "filesystem_free_bytes")) + " / " + formatMaybeBytes(workspaceValue(storage, "reserve_bytes"))],
    [t("admin.workspace.metric.admissible_free"), formatMaybeBytes(workspaceValue(storage, "admissible_free_bytes", "usable_capacity_bytes"))],
    [t("admin.workspace.metric.allocated"), formatMaybeBytes(workspaceValue(storage, "workspace_allocated_bytes", "actual_allocation_bytes"))],
    [t("admin.workspace.metric.apparent"), formatMaybeBytes(workspaceValue(storage, "workspace_apparent_bytes", "apparent_allocation_bytes"))],
    [t("admin.workspace.metric.measurement_status"), workspaceValue(storage, "measurement_status")],
    [t("admin.workspace.metric.measurement_reason"), workspaceValue(storage, "measurement_reason")],
    [t("admin.workspace.metric.measurement_source"), workspaceValue(storage, "measurement_source")],
    [t("admin.workspace.metric.measured_at"), when(workspaceValue(storage, "measured_at"))],
  ].map(([name, value]) => '<div class="workspace-runtime-card"><small>' + esc(name) + '</small><strong>' + esc(value) + '</strong></div>').join("");
}

function renderWorkspaces(data) {
  const body = $("#workspaces-body");
  if (!body) return;
  const rows = Array.isArray(data && data.workspaces) ? data.workspaces : state.workspaces;
  if (!rows.length) { body.innerHTML = '<p class="muted">' + esc(t("admin.workspace.empty")) + '</p>'; updateWorkspaceSelection(); return; }
  body.innerHTML = '<table><thead><tr><th><span class="sr-only">' + esc(t("admin.workspace.column.select")) + '</span></th><th>' + esc(t("admin.workspace.column.credential")) + '</th><th>' + esc(t("admin.workspace.column.state")) + '</th><th>' + esc(t("admin.workspace.column.activity")) + '</th><th>' + esc(t("admin.workspace.column.usage")) + '</th><th>' + esc(t("admin.workspace.column.paths")) + '</th><th>' + esc(t("admin.workspace.column.actions")) + '</th></tr></thead><tbody>' + rows.map((row) => {
    const id = workspaceValue(row, "workspace_id", "id");
    const stateValue = workspaceValue(row, "state", "status");
    const actual = workspaceValue(row, "actual_bytes", "used_bytes");
    const apparent = workspaceValue(row, "apparent_bytes");
    const quota = workspaceValue(row, "quota_bytes");
    const removeAllowed = ["verified", "absent"].includes(row.path_status);
    const stateKey = ({ running: "admin.option.running", stopped: "admin.option.stopped", transition: "admin.option.transition", failed: "admin.option.failed", orphaned: "admin.option.orphaned" })[String(stateValue).toLowerCase()] || "admin.status.unavailable";
    return '<tr data-workspace-id="' + esc(id) + '">' +
      '<td><input type="checkbox" data-workspace-select aria-label="' + esc(t("admin.workspace.column.select")) + ' Workspace ' + esc(id) + '"></td>' +
      '<td><strong>' + esc(workspaceValue(row, "credential_label", "label")) + '</strong><br><small>' + esc(workspaceValue(row, "credential_id", "key_id")) + '</small><br><small>' + esc(workspaceValue(row, "connector_name", "surface_name")) + '</small></td>' +
      '<td><span class="pill ' + esc(String(stateValue).toLowerCase()) + '">' + esc(t(stateKey)) + '</span><br><small>' + esc(t("admin.workspace.field.revision")) + ' ' + esc(workspaceValue(row, "revision")) + '</small></td>' +
      '<td><small>' + esc(t("admin.workspace.field.created")) + ': ' + esc(when(workspaceValue(row, "created_at"))) + '</small><br><small>' + esc(t("admin.workspace.field.last_activity")) + ': ' + esc(when(workspaceValue(row, "last_activity_at", "last_accessed_at"))) + '</small><br><small>' + esc(t("admin.workspace.field.idle")) + ': ' + esc(workspaceValue(row, "idle_duration_seconds")) + 's · ' + esc(t("admin.workspace.field.lease")) + ': ' + esc(when(workspaceValue(row, "lease_expires_at"))) + '</small><br><small>' + esc(t("admin.workspace.field.delete_due")) + ': ' + esc(when(workspaceValue(row, "deletion_due_at", "expiry_at"))) + '</small><br><small>' + esc(row.pinned ? t("admin.workspace.field.pinned") : workspaceValue(row, "retention")) + '</small></td>' +
      '<td><small>' + esc(t("admin.workspace.field.actual")) + ': ' + esc(formatMaybeBytes(actual)) + '</small><br><small>' + esc(t("admin.workspace.field.apparent")) + ': ' + esc(formatMaybeBytes(apparent)) + '</small><br><small>' + esc(actual === "—" || quota === "—" ? t("admin.status.unavailable") : formatMaybeBytes(actual) + " / " + formatMaybeBytes(quota)) + '</small><br><small>' + esc(workspaceValue(row, "quota_percent")) + '% · ' + esc(workspaceValue(row, "usage_status")) + '</small></td>' +
      '<td><code>' + esc(workspaceValue(row, "host_path")) + '</code><br><small>' + esc(t("admin.workspace.field.path")) + ': ' + esc(workspaceValue(row, "path_status")) + '</small><br><small>' + esc(t("admin.workspace.field.container")) + ': ' + esc(workspaceValue(row, "container_path")) + '</small></td>' +
      '<td><small>' + esc(t("admin.workspace.field.error")) + ': ' + esc(workspaceValue(row, "last_error_code")) + '</small><br><small>' + esc(t("admin.workspace.field.owner")) + ': ' + esc(workspaceValue(row, "owner_status")) + '</small><div class="actions"><button type="button" data-workspace-op="diagnostics" class="secondary outline">' + esc(t("admin.workspace.action.diagnostics")) + '</button>' + (stateValue === "failed" ? '<button type="button" data-workspace-op="retry" class="secondary outline">' + esc(t("admin.workspace.action.retry")) + '</button>' : '') + '<button type="button" data-workspace-op="' + (stateValue === "running" ? "stop" : "start") + '">' + esc(t(stateValue === "running" ? "admin.workspace.action.stop" : "admin.workspace.action.start")) + '</button><button type="button" data-workspace-op="' + (row.pinned ? "unpin" : "pin") + '" class="secondary outline">' + esc(t(row.pinned ? "admin.workspace.action.unpin" : "admin.workspace.action.pin")) + '</button><button type="button" data-workspace-op="remove" class="contrast outline"' + (removeAllowed ? '' : ' disabled title="' + esc(t("admin.workspace.removal_unavailable")) + '"') + '>' + esc(t("admin.workspace.action.remove")) + '</button></div></td>' +
      '</tr>';
  }).join("") + '</tbody></table><div class="workspace-pagination"><button type="button" class="secondary outline" data-workspace-page="first"' + (state.workspaceCursor || state.workspaceCursorStack.length ? '' : ' disabled') + '>' + esc(t("admin.workspace.page.first")) + '</button><button type="button" class="secondary outline" data-workspace-page="previous"' + (state.workspaceCursorStack.length ? '' : ' disabled') + '>' + esc(t("admin.workspace.page.previous")) + '</button>' + (state.workspaceNextCursor ? '<button type="button" id="workspace-next-page" class="secondary outline" data-workspace-page="next" data-workspace-next-cursor="' + esc(state.workspaceNextCursor) + '">' + esc(t("admin.workspace.page.next")) + '</button>' : '<small class="muted">' + esc(t("admin.workspace.page.end")) + '</small>') + '</div>';
  updateWorkspaceSelection();
}

function renderWorkspaceSettings(data) {
  const settings = data && data.settings || state.workspaceSettings || {};
  const fields = {
    "#workspace-retention-days": "retention_days", "#workspace-quota-bytes": "quota_bytes",
    "#workspace-idle-stop": "idle_stop_seconds", "#workspace-host-reserve": "host_reserve_bytes",
    "#workspace-warning-threshold": "warning_threshold_percent", "#workspace-max-running": "max_running_workspaces",
  };
  Object.entries(fields).forEach(([selector, key]) => { const el = $(selector); if (el && settings[key] !== undefined) el.value = settings[key]; });
  const mode = $("#workspace-network-mode"); if (mode && settings.network_mode) mode.value = settings.network_mode;
  const storedRules = Array.isArray(settings.network_rules) ? settings.network_rules : [];
  const editorRules = storedRules.map((rule) => workspaceNetworkEditorShape(rule));
  state.workspaceNetworkStoredRules = storedRules;
  state.workspaceNetworkEditorRules = editorRules;
  state.workspaceNetworkLegacyOverrides = new Set();
  state.workspaceNetworkEditorBaseline = JSON.stringify(editorRules);
  renderWorkspaceNetworkRules();
  const unavailable = storedRules.filter((rule) => rule &&
    (rule.availability === "unavailable" || (Array.isArray(rule.protocols) && rule.protocols.length === 1)) &&
    !state.workspaceNetworkLegacyOverrides.has(workspaceNetworkShapeKey(rule)));
  const networkStatus = $("#workspace-network-status");
  if (networkStatus) {
    networkStatus.textContent = unavailable.length
      ? t(unavailable.length === 1 ? "admin.workspace.network.legacy_unavailable_one" : "admin.workspace.network.legacy_unavailable_many", { domains: unavailable.map((rule) => rule.domain || t("admin.status.unknown")).join(", ") })
      : t("admin.workspace.network.port_guidance");
  }
  const brave = $("#workspace-brave-enabled"); if (brave && settings.brave_enabled !== undefined) brave.checked = Boolean(settings.brave_enabled);
  const label = $("#workspace-settings-status"); if (label) label.textContent = t("admin.workspace.revision", { revision: workspaceValue(settings, "revision") });
}

function workspaceNetworkEditorShape(rule) {
  return {
    domain: typeof rule?.domain === "string" ? rule.domain : "",
    ports: Array.isArray(rule?.ports) ? rule.ports : [],
    suffix: Boolean(rule?.suffix),
  };
}

function renderWorkspaceNetworkRules() {
  const list = $("#workspace-network-rule-list");
  if (!list) return;
  const stored = state.workspaceNetworkStoredRules || [];
  list.innerHTML = state.workspaceNetworkEditorRules.map((rule, index) => {
    const legacy = stored.find((item) => item && (item.availability === "unavailable" ||
      (Array.isArray(item.protocols) && item.protocols.length === 1)) &&
      workspaceNetworkShapeKey(item) === workspaceNetworkShapeKey(rule));
    const legacyKey = legacy && workspaceNetworkShapeKey(legacy);
    const legacyEnabled = legacyKey && state.workspaceNetworkLegacyOverrides.has(legacyKey);
    const legacyText = legacy && !legacyEnabled
      ? '<small class="muted">' + esc(t("admin.workspace.network.legacy_rule_unavailable")) + '</small><button type="button" class="secondary outline" data-network-enable-legacy>' + esc(t("admin.workspace.network.enable_http_https")) + '</button>'
      : '';
    return '<div class="workspace-network-rule" data-network-rule-index="' + index + '">' +
      '<label>' + esc(t("admin.workspace.network.domain")) + ' <input type="text" data-network-domain value="' + esc(rule.domain) + '" placeholder="example.com" required></label>' +
      '<label>' + esc(t("admin.workspace.network.match")) + ' <select data-network-suffix><option value="exact"' + (rule.suffix ? '' : ' selected') + '>' + esc(t("admin.workspace.network.exact_domain")) + '</option><option value="suffix"' + (rule.suffix ? ' selected' : '') + '>' + esc(t("admin.workspace.network.domain_and_subdomains")) + '</option></select></label>' +
      '<label>' + esc(t("admin.workspace.network.tcp_ports")) + ' <input type="text" data-network-ports value="' + esc(rule.ports.join(', ')) + '" placeholder="443, 8443" required></label>' +
      '<button type="button" class="secondary outline" data-network-remove>' + esc(t("admin.workspace.network.remove")) + '</button>' + legacyText +
      '</div>';
  }).join("");
  syncWorkspaceNetworkRules();
}

function readWorkspaceNetworkRules() {
  return Array.from(document.querySelectorAll("[data-network-rule-index]")).map((row) => {
    const domain = row.querySelector("[data-network-domain]")?.value.trim() || "";
    const rawPorts = row.querySelector("[data-network-ports]")?.value.trim() || "";
    const pieces = rawPorts.split(",").map((value) => value.trim());
    if (!rawPorts || pieces.some((value) => !/^\d+$/.test(value))) {
      throw new Error(t("admin.workspace.network.ports_integers", { domain: domain || t("admin.workspace.network.each_domain") }));
    }
    const ports = pieces.map((value) => Number(value));
    if (ports.some((value) => value < 1 || value > 65535)) {
      throw new Error(t("admin.workspace.network.ports_range", { domain: domain || t("admin.workspace.network.each_domain") }));
    }
    return { domain, suffix: row.querySelector("[data-network-suffix]")?.value === "suffix", ports };
  });
}

function syncWorkspaceNetworkRules() {
  const rules = readWorkspaceNetworkRules();
  state.workspaceNetworkEditorRules = rules;
  const hidden = $("#workspace-network-rules");
  if (hidden) hidden.value = JSON.stringify(rules);
  return rules;
}

function workspaceNetworkShapeKey(rule) {
  const shape = workspaceNetworkEditorShape(rule);
  return JSON.stringify({ domain: shape.domain, ports: shape.ports, suffix: shape.suffix });
}

function workspaceNetworkPayload(editorRules) {
  return editorRules.map((rule) => {
    const shape = workspaceNetworkEditorShape(rule);
    const legacy = state.workspaceNetworkStoredRules.find((stored) =>
      stored && (stored.availability === "unavailable" ||
        (Array.isArray(stored.protocols) && stored.protocols.length === 1)) &&
        workspaceNetworkShapeKey(stored) === workspaceNetworkShapeKey(shape));
    const legacyKey = legacy && workspaceNetworkShapeKey(legacy);
    const explicitlyEnabled = legacyKey && state.workspaceNetworkLegacyOverrides.has(legacyKey);
    return legacy && !explicitlyEnabled && Array.isArray(legacy.protocols) && legacy.protocols.length === 1
      ? { ...shape, protocols: legacy.protocols.slice() }
      : { ...shape, protocols: ["http", "https"] };
  });
}

function updateWorkspaceSelection() {
  const count = document.querySelectorAll("[data-workspace-select]:checked").length;
  const button = $("#workspace-bulk-remove"); if (button) button.disabled = count === 0;
  const label = $("#workspace-selection-summary"); if (label) label.textContent = count ? t("admin.workspace.selected_count", { count: window.CognitaAdminLocale.number(count) }) : "";
}

function renderWorkspaceConnectors(data) {
  const body = $("#workspace-connectors-body");
  if (!body) return;
  const records = Array.isArray(data && data.workspace_connectors) ? data.workspace_connectors : state.workspaceConnectors;
  if (!records.length) { body.innerHTML = '<p class="muted">' + esc(t("admin.workspaces.no_workspace_connectors")) + '</p>'; return; }
  body.innerHTML = records.map((surface) =>
    '<article class="connector-card workspace-connector-card" data-workspace-surface-id="' + esc(surface.id || surface.surface_id || "") + '"><header><strong>' + esc(surface.display_name || surface.name || "Workspace") + '</strong><span>' + esc(t(surface.workspace_requested ? "admin.workspace_connector.requested" : "admin.workspace_connector.disabled")) + (surface.workspace_reason === "host_workspace_disabled" ? " · " + esc(t("admin.connectors.not_effective_host")) : (surface.workspace_effective ? " · " + esc(t("admin.workspace_connector.effective")) : "")) + '</span></header>' +
    '<p><code>' + esc(surface.slug || "") + '</code> · ' + esc(t("admin.workspace_connector.contract_revision", { revision: surface.revision == null ? "—" : window.CognitaAdminLocale.number(surface.revision) })) + '</p>' +
    '<div class="connector-url-row"><label>' + esc(t("admin.connectors.stable_url.title")) + '<input readonly value="' + esc(surface.url || "") + '"></label><button type="button" class="secondary outline" data-workspace-action="copy" data-url="' + esc(surface.url || "") + '">' + esc(t("admin.connectors.copy_url")) + '</button></div>' +
    '<div class="actions"><button type="button" class="secondary outline" data-workspace-action="credentials">' + esc(t("admin.connectors.private_keys")) + '</button><button type="button" class="contrast outline" data-workspace-action="delete">' + esc(t("admin.workspace_connector.disable")) + '</button></div><div class="workspace-credentials" hidden></div></article>'
  ).join("");
}

async function loadConnectors(cached = null, preloaded = false) {
  try {
    const data = preloaded ? cached : (cached || await api("/api/connectors"));
    state.connectors = Array.isArray(data.connectors) ? data.connectors : [];
    state.connectorsRevision = Number.isInteger(data.revision) ? data.revision : 0;
    if (!state.connectors.some((item) => String(item.id) === String(state.selectedConnectorId))) {
      const safe = state.connectors.find((item) => item.enabled) || state.connectors[0];
      state.selectedConnectorId = safe ? safe.id : null;
    }
    $("#connectors-revision").textContent = t("admin.connectors.policy_revision", { revision: window.CognitaAdminLocale.number(state.connectorsRevision) });
    renderConnectors();
    syncConnectorSelectors();
    const route = normalizedConnectorRoute(routeForHash(window.location.hash));
    state.selectedConnectorId = route.connectorId;
    const canonical = CognitaAdminState.routeFragment(route);
    if (window.location.hash !== canonical) window.history.replaceState(null, "", canonical);
    // This render is itself the result of preload/revalidation.  Starting
    // another route revalidation here would recursively refetch every
    // connector slice without bound.
    activateShellRoute(route, false, false);
    return data;
  } catch (err) {
    $("#connectors-revision").textContent = t("admin.connectors.unavailable");
    $("#connectors-body").innerHTML = '<p role="alert">' + esc(t("admin.connectors.load_failed")) + ': ' +
      esc(inlineErrorText(err)) + "</p>";
    return null;
  }
}

async function loadPublicBaseUrl(cached = null) {
  try {
    const data = cached || await api("/api/settings/public-base-url");
    state.publicBaseUrl = data || {};
    const input = $("#public-base-url");
    if (input && document.activeElement !== input) input.value = data.public_base_url || "";
    const status = $("#public-url-status");
    if (status) status.textContent = data.source === "admin" ? t("admin.settings.saved_by_admin") : t("admin.settings.deployment_default");
    return data;
  } catch (err) {
    const status = $("#public-url-status");
    if (status) status.textContent = t("admin.status.unavailable");
    return null;
  }
}

function setConnectorView(view, focus = false) {
  const selected = ["settings", "clients", "access", "transfer"].includes(view) ? view : "settings";
  state.connectorView = selected;
  const connector = selectedConnector();
  if (window.adminNavigate) window.adminNavigate(connector ? CognitaAdminState.routeFragment(connectorRoute(connector.id, selected)) : "#connectors/add");
  if (focus) document.querySelector(`#connector-tab-${selected}`)?.focus();
}

function initConnectorSubtabs() {
  // Connector navigation is wired as an independent shell tablist in
  // initAdminShell. This function remains as a compatibility seam for older
  // integration fixtures and deliberately does not create another keyboard group.
}

function resetConnectorEditor(options = {}) {
  state.connectorCreateDraft = null;
  state.editingConnectorId = null;
  if (state.selectedConnectorId != null) state.connectorSettingsDrafts.delete(String(state.selectedConnectorId));
  $("#connector-form").reset();
  $("#connector-enabled").checked = true;
  // 13.0.1: Workspace tools and transfer are on by default for a new connector.
  $("#connector-workspace-enabled").checked = true;
  $("#connector-transfer-default").value = "allow";
  $("#connector-high-trust-confirm").checked = true;
  $("#connector-editor-title").textContent = t("admin.connectors.add");
  $("#connector-submit").textContent = t("admin.connectors.add");
  $("#connector-cancel").hidden = true;
  renderConnectorEditorProjects(selectedConnector());
  if (options.keepSelection) return;
  const connector = selectedConnector() || state.connectors.find((item) => String(item.id) === String(state.lastExistingConnectorId));
  if (connector) {
    state.selectedConnectorId = connector.id;
    if (window.adminNavigate) window.adminNavigate(CognitaAdminState.routeFragment(connectorRoute(connector.id, "settings")));
  } else if (window.adminNavigate) window.adminNavigate("#connectors/add");
}

function beginConnectorEdit(connector) {
  captureConnectorDraft();
  state.editingConnectorId = connector.id;
  state.selectedConnectorId = connector.id;
  if (window.adminNavigate) window.adminNavigate(CognitaAdminState.routeFragment(connectorRoute(connector.id, "settings")));
  $("#connector-editor").open = true;
  $("#connector-editor-title").textContent = t("admin.connectors.edit_title", { name: connector.name });
  $("#connector-submit").textContent = t("admin.connectors.save");
  $("#connector-cancel").hidden = false;
  $("#connector-name").value = connector.name;
  $("#connector-enabled").checked = Boolean(connector.enabled);
  $("#connector-workspace-enabled").checked = Boolean(connector.workspace_enabled);
  $("#connector-transfer-default").value = connector.default_workspace_transfer || "allow";
  $("#connector-high-trust-confirm").checked = false;
  document.querySelectorAll('input[name="project-mode"]').forEach((radio) => {
    radio.checked = radio.value === connector.project_mode;
  });
  $("#connector-default-readonly").checked = connector.default_access === "read";
  renderConnectorEditorProjects(connector);
  $("#connector-editor").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function handleConnectorError(err) {
  if (err.reason === "revision_conflict" || err.status === 409) {
    const editingId = state.editingConnectorId;
    if (state.selectedConnectorId != null) state.connectorDrafts.delete(String(state.selectedConnectorId));
    await uiNotice({
      title: t("admin.connectors.changed_elsewhere.title"),
      body: t("admin.connectors.changed_elsewhere.body"),
    });
    await loadConnectors();
    if (editingId) {
      const current = state.connectors.find((item) => item.id === editingId);
      if (current) beginConnectorEdit(current);
      else resetConnectorEditor();
    }
    return;
  }
  await uiNotice({ title: t("admin.connectors.change_failed"), body: err.message, technicalDetail: err.technicalDetail });
}

function connectionSummary(grant) {
  const connector = grant.connector ||
    state.connectors.find((item) => item.url && item.url === grant.resource);
  if (connector) {
    const connectorName = connector.name || t("admin.oauth.deleted_connector");
    const connectorState = connector.enabled === false ? ` — ${t("admin.oauth.disabled")}` : "";
    const projects = Array.isArray(connector.projects) ? connector.projects :
      state.projects.filter((project) => project.enabled).map((project) => ({
        name: project.name,
        access: connector.project_mode === "selected"
          ? connector.project_access && connector.project_access[project.name]
          : (connector.project_access && connector.project_access[project.name]) ||
            (project.exclude_from_default_permissions ? null : connector.default_access),
      })).filter((project) => project.access);
    const access = projects.length
      ? projects.map((project) => project.name + ": " +
        (project.access === "read" ? t("admin.oauth.read_only") : t("admin.oauth.read_write"))).join(", ")
      : t("admin.oauth.no_accessible_projects");
    return connectorName + " (" + (connector.id || "connector") + ")" +
      connectorState + "\n" + access;
  }
  return grant.project ? t("admin.oauth.legacy_project", { project: grant.project }) : t("admin.oauth.summary_unavailable");
}

function grantConnectorId(grant) {
  const linked = grant && grant.connector;
  const candidate = grant && (grant.connector_id || grant.surface_id) ||
    (linked && (linked.id || linked.connector_id));
  return candidate == null ? null : String(candidate);
}

async function loadOAuthV9(cached = null) {
  const status = $("#oauth-status");
  const problems = $("#oauth-problems");
  const body = $("#grants-body");
  try {
    const result = cached || await Promise.all([api("/api/oauth/status"), api("/api/oauth/grants")]);
    const fromCache = Boolean(cached && Object.prototype.hasOwnProperty.call(cached, "status"));
    const oauthState = fromCache ? result.status : result[0];
    const data = fromCache ? result.grants : (result.data || result[1]);
    status.textContent = oauthState.enabled
      ? (oauthState.ready ? t("admin.option.enabled") : t("admin.oauth.configuration_required")) : t("admin.oauth.disabled");
    problems.innerHTML = oauthState.problems && oauthState.problems.length
      ? '<p role="alert"><strong>' + esc(t("admin.oauth.not_ready")) + "</strong><br>" +
        esc(t("admin.technical_detail")) + ": " + oauthState.problems.map(esc).join("; ") + "</p>"
      : "";
    const allGrants = Array.isArray(data.grants) ? data.grants : [];
    const selectedId = state.selectedConnectorId == null ? null : String(state.selectedConnectorId);
    // Grants are scoped by the stable connector identity supplied by the Admin
    // API. Never infer ownership from display names or resource URL text.
    const grants = selectedId == null ? [] : allGrants.filter((grant) => grantConnectorId(grant) === selectedId);
    body.innerHTML = grants.length ? grants.map((grant) =>
      '<tr data-grant="' + esc(grant.id) + '"><td><strong>' +
      esc(grant.client_name) + '</strong><br><small class="muted">' +
      esc(grant.client_id) + '</small></td><td class="connector-summary">' +
      esc(connectionSummary(grant)) + "</td><td>" + esc(when(grant.created_at)) +
      "</td><td>" + esc(when(grant.last_used_at)) +
      '</td><td><button class="contrast outline" data-revoke="' +
      esc(grant.id) + '">' + esc(t("admin.oauth.revoke")) + "</button></td></tr>"
    ).join("") : '<tr><td colspan="5"><em>' + esc(t("admin.oauth.no_clients")) + "</em></td></tr>";
    $("#revoke-all-grants").disabled = !grants.length;
  } catch (err) {
    status.textContent = t("admin.status.unavailable");
    body.innerHTML = "<tr><td colspan=\"5\">" + esc(inlineErrorText(err)) + "</td></tr>";
  }
}

// The legacy functions remain above for compatibility with older fixtures; all
// UI entry points below use the 9.0 views and API contract.
loadProjects = loadProjectsV9;
loadOAuth = loadOAuthV9;

async function submitConnectorForm(event) {
  event.preventDefault();
  event.stopImmediatePropagation();
  const button = $("#connector-submit");
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  try {
    const id = state.editingConnectorId;
    const method = id ? "PATCH" : "POST";
    const path = id ? "/api/connectors/" + encodeURIComponent(id) : "/api/connectors";
    const result = await api(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_revision: state.connectorsRevision,
        name: $("#connector-name").value.trim(),
        enabled: $("#connector-enabled").checked,
        workspace_enabled: $("#connector-workspace-enabled").checked,
        default_workspace_transfer: $("#connector-transfer-default").value,
        confirm_high_trust: $("#connector-high-trust-confirm").checked,
      }),
    });
    const created = result && (result.connector || result);
    const createdId = created && created.id ? created.id : null;
    if (createdId) state.selectedConnectorId = createdId;
    if (id) state.connectorSettingsDrafts.delete(String(id));
    resetConnectorEditor({ keepSelection: Boolean(createdId && !id) });
    await refreshAdminMutation(id ? "connector:update" : "connector:create");
    if (createdId && !id) {
      state.selectedConnectorId = createdId;
      state.lastExistingConnectorId = createdId;
      const createdRoute = connectorRoute(createdId, "settings");
      window.history.replaceState(null, "", CognitaAdminState.routeFragment(createdRoute));
      activateShellRoute(createdRoute);
    }
    const createdTab = document.querySelector('[data-connector-id="' + CSS.escape(String(state.selectedConnectorId || "")) + '"]');
    if (createdTab) createdTab.focus();
  } catch (err) {
    await handleConnectorError(err);
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

$("#connector-form").addEventListener("submit", submitConnectorForm);
document.querySelectorAll('input[name="project-mode"]').forEach((radio) =>
  radio.addEventListener("change", () => {
    captureConnectorDraft();
    renderConnectorEditorProjects(selectedConnector());
  }));
$("#connector-cancel").addEventListener("click", resetConnectorEditor);

async function saveConnectorPolicy(kind) {
  const connector = selectedConnector();
  if (!connector) {
    await uiNotice({ title: t("admin.connectors.no_selected.title"), body: t("admin.connectors.no_selected.body") });
    return;
  }
  const button = $(kind === "access" ? "#connector-access-save" : "#connector-transfer-save");
  if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
  try {
    captureConnectorDraft();
    const draft = connectorDraft(connector);
    const payload = kind === "access" ? {
      expected_revision: state.connectorsRevision,
      project_mode: draft.access.project_mode,
      default_access: draft.access.default_access,
      project_access: draft.access.project_access,
    } : {
      expected_revision: state.connectorsRevision,
      default_workspace_transfer: draft.transfer.default_workspace_transfer,
      project_transfer: draft.transfer.project_transfer,
      confirm_high_trust: draft.transfer.confirm_high_trust,
    };
    await api("/api/connectors/" + encodeURIComponent(connector.id), {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    state.connectorDrafts.delete(String(connector.id));
    await refreshAdminMutation("connector:update");
    selectConnector(connector.id);
    await uiNotice({ title: t("admin.connectors.policy_saved.title"), body: t(kind === "access" ? "admin.connectors.access_saved" : "admin.connectors.transfer_saved") });
  } catch (err) {
    await handleConnectorError(err);
  } finally {
    if (button) { button.disabled = false; button.removeAttribute("aria-busy"); }
  }
}

$("#connector-access-save")?.addEventListener("click", () => saveConnectorPolicy("access"));
$("#connector-transfer-save")?.addEventListener("click", () => saveConnectorPolicy("transfer"));
document.addEventListener("click", (event) => {
  const edit = event.target.closest("[data-connector-edit]");
  if (edit) {
    const connector = state.connectors.find((item) => String(item.id) === String(edit.dataset.connectorEdit));
    if (connector) beginConnectorEdit(connector);
    return;
  }
  const remove = event.target.closest("[data-connector-delete]");
  if (remove) {
    const connector = state.connectors.find((item) => String(item.id) === String(remove.dataset.connectorDelete));
    if (connector) deleteConnector(connector);
    return;
  }
  const copy = event.target.closest("[data-connector-copy-url]");
  if (copy) { copyText(copy.dataset.url || "", copy); return; }
  if (event.target.closest("[data-go-connector-setup]") && window.adminNavigate) {
    const connector = selectedConnector();
    window.adminNavigate(connector ? CognitaAdminState.routeFragment(connectorRoute(connector.id, "settings")) : "#connectors/add");
  }
});

$("#workspace-connector-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!$("#workspace-connector-confirm").checked) {
    await uiNotice({ title: t("admin.workspace_connector.confirmation_required"), body: t("admin.workspace_connector.create_warning") });
    return;
  }
  try {
    await api("/api/workspace-connectors", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_revision: state.workspaceConnectorsRevision,
        display_name: $("#workspace-connector-name").value.trim(),
        slug: $("#workspace-connector-slug").value.trim(),
        enabled: $("#workspace-connector-enabled").checked,
        confirm_high_trust: true,
      }),
    });
    event.target.reset();
    await refreshAdminMutation("workspace-connector:create");
  } catch (err) { await uiNotice({ title: t("admin.workspace_connector.create_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});

$("#workspace-connectors-body").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-workspace-action]");
  if (!button) return;
  const card = button.closest("[data-workspace-surface-id]");
  if (!card) return;
  const id = card.dataset.workspaceSurfaceId;
  if (button.dataset.workspaceAction === "copy") { await copyText(button.dataset.url, button); return; }
  if (button.dataset.workspaceAction === "credentials") {
    const panel = card.querySelector(".workspace-credentials");
    panel.hidden = !panel.hidden;
    if (!panel.hidden) await renderCredentialList("workspace", id, panel);
    return;
  }
  const confirmation = await uiConfirm({ title: t("admin.workspace_connector.disable_title"), body: t("admin.workspace_connector.disable_body"), confirmLabel: t("admin.workspace_connector.disable"), danger: true });
  if (!confirmation.ok) return;
  const current = state.workspaceConnectors.find((item) => String(item.id || item.surface_id) === String(id));
  try {
    await api("/api/workspace-connectors/" + encodeURIComponent(id), { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: current && current.revision || state.workspaceConnectorsRevision, confirm: true }) });
    await refreshAdminMutation("workspace-connector:delete");
  } catch (err) { await uiNotice({ title: t("admin.workspace_connector.disable_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});

document.addEventListener("click", async (event) => {
  const add = event.target.closest("button[data-credential-add]");
  if (add) {
    const form = await uiForm({
      title: t("admin.credential.add.title"),
      body: t("admin.credential.add.body"),
      confirmLabel: t("admin.credential.add.confirm"),
      fields: [
        { name: "label", label: t("admin.credential.field.label"), type: "text", maxlength: 120 },
        { name: "current_password", label: t("admin.credential.field.admin_password"), type: "password", maxlength: 512 },
      ],
    });
    if (!form.ok) return;
    const label = String(form.values.label || "").trim();
    const currentPassword = String(form.values.current_password || "");
    if (!label || !currentPassword) {
      await uiNotice({ title: t("admin.credential.missing.title"), body: t("admin.credential.missing.body") });
      return;
    }
    const kind = add.dataset.credentialAdd;
    const prefix = kind === "workspace" ? "/api/workspace-connectors/" : "/api/connectors/";
    const surfaceId = add.dataset.surfaceId;
    try {
      const { panel, list } = credentialPanelOf(add);
      const expectedRevision = Number(panel.dataset.credentialRevision || 0);
      const result = await api(prefix + encodeURIComponent(surfaceId) + "/credentials", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: expectedRevision, label, current_password: currentPassword }) });
      await showCredentialSecret(result, t("admin.credential.created"));
      await renderCredentialList(kind, surfaceId, list);
    } catch (err) { await uiNotice({ title: t("admin.credential.create_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
    return;
  }
  const action = event.target.closest("button[data-credential-action]");
  if (!action) return;
  const kind = action.dataset.surfaceKind;
  const prefix = kind === "workspace" ? "/api/workspace-connectors/" : "/api/connectors/";
  const credentialPath = prefix + encodeURIComponent(action.dataset.surfaceId) + "/credentials/" + encodeURIComponent(action.dataset.credentialId);
  const path = credentialPath + "/" + action.dataset.credentialAction;
  try {
    if (action.dataset.credentialAction === "setup") {
      const proof = await askAdminPassword(t("admin.credential.setup.title"), t("admin.credential.setup.password_body"), t("admin.credential.action.reveal"));
      if (!proof) return;
      const result = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ route_strategy: "stable", provider: "provider-neutral", current_password: proof }) });
      const setup = action.closest(".credential-row")?.querySelector(".connection-instructions");
      if (!setup) return;
      const setupJson = JSON.stringify(result.sillytavern || result.sillytavern_json || result, null, 2);
      setup.hidden = false;
      setup.innerHTML = '<p><strong>' + esc(t("admin.credential.setup.heading")) + '</strong></p><p><strong>' + esc(t("admin.credential.setup.url")) + '</strong> <code>' + esc(result.url || "") + '</code> <button type="button" class="secondary outline" data-copy-setup-url>' + esc(t("admin.credential.setup.copy_url")) + '</button></p><pre><code>' + esc(setupJson) + '</code></pre><button type="button" class="secondary outline" data-copy-setup>' + esc(t("admin.credential.setup.copy_config")) + '</button>';
      setup.querySelector("[data-copy-setup]").addEventListener("click", (copyEvent) => copyText(setupJson, copyEvent.currentTarget));
      setup.querySelector("[data-copy-setup-url]").addEventListener("click", (copyEvent) => copyText(result.url || "", copyEvent.currentTarget));
      window.adminNavigate("#workspaces/connectors");
    } else {
      const { panel: credentialPanel, list: credentialList } = credentialPanelOf(action);
      const expectedRevision = Number(credentialPanel.dataset.credentialRevision || 0);
      const requestBody = { expected_revision: expectedRevision, confirm: action.dataset.credentialAction !== "revoke" };
      let requestMethod = "POST";
      if (action.dataset.credentialAction === "delete") {
        // The credential panel may be filtered, paged, or older than the
        // current inventory. Re-read both the credential revision and the
        // credential-indexed Workspace immediately before asking for the
        // destructive retention choice; never trust row data for this target.
        const credentialIndex = await api(prefix + encodeURIComponent(action.dataset.surfaceId) + "/credentials");
        const currentCredential = (Array.isArray(credentialIndex && credentialIndex.credentials)
          ? credentialIndex.credentials : []).find((item) => String(item.credential_id || item.key_id || "") === String(action.dataset.credentialId));
        if (!currentCredential) {
          // A row the server no longer lists is stale,
          // not an error to read -- redraw the list from the server, which
          // drops it, and say so.
          await renderCredentialList(kind, action.dataset.surfaceId, credentialList);
          await uiNotice({ title: t("admin.credential.list_refreshed.title"), body: t("admin.credential.list_refreshed.body") });
          return;
        }
        if (!Number.isInteger(credentialIndex && credentialIndex.revision)) {
          throw new Error(t("admin.credential.revision_unavailable"));
        }
        requestBody.expected_revision = credentialIndex.revision;
        const authoritative = await fetchCredentialWorkspaceTarget(action.dataset.credentialId);
        const liveWorkspace = authoritative.workspace;
        const retentionForm = await uiForm({
          title: t("admin.credential.delete.title"),
          body: t("admin.credential.delete.body"),
          confirmLabel: t("admin.credential.delete.continue"),
          fields: [{
            name: "retention", label: t("admin.credential.delete.retention"), type: "select", value: "normal",
            options: [
              { value: "normal", label: t("admin.credential.delete.normal") },
              { value: "keep", label: t("admin.credential.delete.keep") },
              { value: "delete_now", label: t("admin.credential.delete.now") },
            ],
          }],
        });
        if (!retentionForm.ok) return;
        const retention = retentionForm.values.retention;
        if (!["keep", "delete_now", "normal"].includes(retention)) {
          await uiNotice({ title: t("admin.credential.delete.invalid.title"), body: t("admin.credential.delete.invalid.body") });
          return;
        }
        const workspaceId = liveWorkspace && (liveWorkspace.workspace_id || liveWorkspace.id);
        const workspaceState = liveWorkspace && (liveWorkspace.state || liveWorkspace.status);
        const workspaceOwner = liveWorkspace && liveWorkspace.owner_status;
        const workspaceRevision = liveWorkspace && liveWorkspace.revision;
        if (liveWorkspace && !Number.isInteger(workspaceRevision)) {
          throw new Error(t("admin.workspace.revision_unavailable"));
        }
        const target = workspaceId || t("admin.credential.target.lazy");
        const stateText = workspaceState || t("admin.credential.target.not_created");
        const ownerText = workspaceOwner || t("admin.credential.target.not_applicable");
        const retentionConsequence = retention === "normal"
          ? t("admin.credential.consequence.normal")
          : retention === "keep"
            ? t("admin.credential.consequence.keep")
            : t("admin.credential.consequence.delete_now");
        const confirmation = await uiConfirm({ title: t("admin.credential.delete.confirm.title"), body: t("admin.credential.delete.confirm.body", { credential: action.dataset.credentialId, target, state: stateText, owner: ownerText, revision: authoritative.revision ?? t("admin.credential.target.not_reported"), retention: t(retention === "normal" ? "admin.credential.delete.normal" : retention === "keep" ? "admin.credential.delete.keep" : "admin.credential.delete.now"), consequence: retentionConsequence }), confirmLabel: t("admin.credential.action.delete"), danger: true });
        if (!confirmation.ok) return;
        requestMethod = "DELETE";
        requestBody.confirm = true;
        requestBody.retention = retention;
        requestBody.workspace_id = workspaceId || null;
        requestBody.workspace_revision = liveWorkspace ? workspaceRevision : null;
      }
      if (["reveal", "rotate"].includes(action.dataset.credentialAction)) {
        const revealing = action.dataset.credentialAction === "reveal";
        const verb = t(revealing ? "admin.credential.action.reveal" : "admin.credential.action.rotate");
        requestBody.current_password = await askAdminPassword(
          t(revealing ? "admin.credential.reveal.title" : "admin.credential.rotate.title"),
          t(revealing ? "admin.credential.reveal.password_body" : "admin.credential.rotate.password_body"),
          verb,
        );
        if (!requestBody.current_password) return;
      }
      const requestPath = action.dataset.credentialAction === "delete" ? credentialPath : path;
      const result = await api(requestPath, { method: requestMethod, headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody) });
      if (result.secret) await showCredentialSecret(result, t("admin.credential.secret.title"));
      await renderCredentialList(kind, action.dataset.surfaceId, credentialList);
    }
  } catch (err) { await uiNotice({ title: t("admin.credential.action_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});

const addForm = $("#add-form");
$("#project-settings-form").addEventListener("submit", saveProjectSettings);
$("#project-settings-cancel").addEventListener("click", () => $("#project-settings-dialog").close());
const documentsPathInput = addForm.elements.documents_dir;
const documentsPathButton = $("#test-documents-path");
const documentsPathStatus = $("#documents-path-status");

function clearDocumentsPathStatus() {
  documentsPathStatus.textContent = "";
  documentsPathStatus.style.color = "";
}

documentsPathInput.addEventListener("input", clearDocumentsPathStatus);

// Installer design 7.2: when the installer bound documents roots into the container
// (GET /api/document-roots is non-empty), the form asks for a root plus a folder inside
// it instead of an absolute path. With no roots, nothing changes.
// 19.2: each root is {path, display}. `path` identifies the root (it is what the server is sent);
// `display` is what a person knows it by (a Windows path on a WSL install, else the path itself).
let documentRoots = [];

function currentDocumentsRoot(form) {
  return documentRoots.length === 1 ? documentRoots[0].path : form.elements.documents_root.value;
}

// 19.2: a folder typed the Windows way (Manuals\Aircraft) becomes Manuals/Aircraft here, for Test
// folder AND Save. The server converts too and still refuses .. and absolute forms afterwards.
function currentDocumentsFolder(form) {
  return form.elements.documents_folder.value.trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
}

// The documents_dir the unchanged POST /api/projects receives.
function currentDocumentsDir(form) {
  if (!documentRoots.length) return form.elements.documents_dir.value.trim();
  const root = currentDocumentsRoot(form);
  const folder = currentDocumentsFolder(form);
  return folder ? root.replace(/[\\/]+$/, "") + "/" + folder : root;
}

async function loadDocumentRoots() {
  try {
    const data = await api("/api/document-roots");
    // 19.2: {path, display} objects. A bare string (an older server) is a root with no separate display.
    documentRoots = Array.isArray(data && data.roots)
      ? data.roots
          .map((r) => (typeof r === "string" ? { path: r, display: r } : r))
          .filter((r) => r && typeof r.path === "string" && r.path)
          .map((r) => ({ path: r.path, display: typeof r.display === "string" && r.display ? r.display : r.path }))
      : [];
  } catch (err) {
    // Not fatal: the absolute-path field keeps working. Say why in the console.
    console.warn("Could not load documents roots; using the absolute-path field:", err.payload?.detail || err.technicalDetail || "request failed");
    documentRoots = [];
  }
  const rootMode = documentRoots.length > 0;
  $("#documents-root-fields").hidden = !rootMode;
  $("#documents-dir-field").hidden = rootMode;
  documentsPathInput.required = !rootMode;
  documentsPathButton.textContent = rootMode ? t("admin.projects.test_folder") : t("admin.projects.test_path");
  const choice = $("#documents-root-choice");
  const single = $("#documents-root-single");
  choice.hidden = !(documentRoots.length > 1);
  single.hidden = documentRoots.length !== 1;
  if (documentRoots.length === 1) $("#documents-root-text").textContent = documentRoots[0].display;
  if (documentRoots.length > 1) {
    const select = addForm.elements.documents_root;
    select.replaceChildren(...documentRoots.map((root) => {
      const option = document.createElement("option");
      option.value = root.path;
      option.textContent = root.display;
      return option;
    }));
  }
  console.info("Documents roots loaded: count=" + documentRoots.length);
}

addForm.elements.documents_folder.addEventListener("input", clearDocumentsPathStatus);
addForm.elements.documents_root.addEventListener("change", clearDocumentsPathStatus);
loadDocumentRoots();

documentsPathButton.addEventListener("click", async () => {
  const rootMode = documentRoots.length > 0;
  const documentsDir = documentsPathInput.value.trim();
  if (!rootMode && !documentsDir) {
    documentsPathStatus.textContent = t("admin.projects.absolute_path_required");
    documentsPathStatus.style.color = "var(--pico-del-color)";
    return;
  }
  documentsPathButton.setAttribute("aria-busy", "true");
  documentsPathButton.disabled = true;
  documentsPathStatus.textContent = t("admin.projects.checking_path");
  documentsPathStatus.style.color = "";
  try {
    const info = await api("/api/projects/path-info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rootMode
        ? { root: currentDocumentsRoot(addForm), folder: currentDocumentsFolder(addForm) }
        : { documents_dir: documentsDir }),
    });
    if (rootMode) {
      // The server's plain sentence is the message; green only when Cognita can read and write.
      // 19.2: name the folder the way a person knows it when that differs from the container path.
      documentsPathStatus.textContent = presentOutcome(info);
      documentsPathStatus.style.color = info.readable && info.writable
        ? "var(--pico-ins-color)" : "var(--pico-del-color)";
      return;
    }
    documentsPathStatus.textContent = presentOutcome(info);
    documentsPathStatus.style.color = "var(--pico-ins-color)";
  } catch (err) {
    documentsPathStatus.textContent = `${t("admin.projects.path_check_failed")}: ${inlineErrorText(err)}`;
    documentsPathStatus.style.color = "var(--pico-del-color)";
  } finally {
    documentsPathButton.removeAttribute("aria-busy");
    documentsPathButton.disabled = false;
  }
});

addForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  event.stopImmediatePropagation();
  const form = event.target;
  const button = form.querySelector("button[type=submit]");
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  try {
    const createdName = form.name.value.trim();
    await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: form.name.value.trim(),
        documents_dir: currentDocumentsDir(form),
        writable: form.writable.checked,
        exclude_from_default_permissions: form.exclude_from_default_permissions.checked,
      }),
    });
    form.reset();
    form.writable.checked = true;
    form.exclude_from_default_permissions.checked = false;
    clearDocumentsPathStatus();
    state.highlightedProject = createdName;
    await refreshAdminMutation("project:create");
    if (window.adminNavigate) window.adminNavigate("#projects/view");
    const createdRow = document.querySelector('tr[data-name="' + CSS.escape(createdName) + '"]');
    if (createdRow) {
      createdRow.classList.add("project-highlight");
      createdRow.setAttribute("tabindex", "-1");
      createdRow.focus({ preventScroll: true });
      createdRow.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    const actions = $("#project-created-actions");
    actions.hidden = false;
    actions.innerHTML = `<strong>${esc(t("admin.projects.created", { name: createdName }))}</strong> <button type="button" data-created-auth>${esc(t("admin.action.configure_authentication"))}</button> <button type="button" class="secondary outline" data-created-another>${esc(t("admin.projects.create_another"))}</button>`;
    actions.querySelector("[data-created-auth]").focus();
    actions.querySelector("[data-created-auth]").addEventListener("click", () => window.adminNavigate("#authentication/" + encodeURIComponent(createdName)));
    actions.querySelector("[data-created-another]").addEventListener("click", () => { actions.hidden = true; window.adminNavigate("#projects/create"); $("#add-form input[name=name]").focus(); });
  } catch (err) {
    await uiNotice({ title: t("admin.projects.add_failed"), body: err.message, technicalDetail: err.technicalDetail });
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
});

$("#refresh-btn").addEventListener("click", async (event) => {
  event.stopImmediatePropagation();
  await Promise.all([loadProjects(), loadConnectors(), loadOAuth()]);
});

async function loadOAuth() {
  return loadOAuthV9();
}

async function clearWatcherQueueAction(name, button) {
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  try {
    const result = await api(`/api/projects/${encodeURIComponent(name)}/watcher/clear-queue`, { method: "POST" });
    await fillDocCount(name);
    await uiNotice({
      title: t("admin.projects.clear_watcher_queue"),
      body: t("admin.projects.clear_watcher_queue_result", {
        cleared_paths: result.cleared_paths,
        active_cancelled: result.active_cancelled ? t("admin.projects.clear_watcher_queue_active_cancelled") : t("admin.projects.clear_watcher_queue_no_active"),
      }),
    });
  } catch (err) {
    await uiNotice({ title: t("admin.projects.clear_watcher_queue_failed"), body: err.message, technicalDetail: err.technicalDetail });
    await fillDocCount(name);
  } finally {
    button.removeAttribute("aria-busy");
  }
}

// ---- event wiring ----

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const btn = form.querySelector("button[type=submit]");
  const payload = {
    name: form.name.value.trim(),
    documents_dir: currentDocumentsDir(form),
    writable: form.writable.checked,
    exclude_from_default_permissions: form.exclude_from_default_permissions.checked,
  };
  btn.setAttribute("aria-busy", "true");
  btn.disabled = true;
  try {
    const info = await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    form.reset();
    form.writable.checked = true;
    form.exclude_from_default_permissions.checked = false;
    const url = info.connector_url || info.connector_path;
    await copyText(url, btn);
    uiNotice({ title: t("admin.projects.added"), body: t("admin.projects.connector_url_copied", { url }) });
    loadProjects();
  } catch (err) {
    uiNotice({ title: t("admin.projects.add_failed"), body: err.message, technicalDetail: err.technicalDetail });
  } finally {
    btn.removeAttribute("aria-busy");
    btn.disabled = false;
  }
});

$("#projects-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const name = btn.closest("tr").dataset.name;
  const act = btn.dataset.act;

  if (act === "settings") {
    const project = state.projects.find((item) => item.name === name);
    if (project) openProjectSettings(project);
  } else if (act === "copy") {
    await copyText(btn.dataset.url, btn);
  } else if (act === "connections") {
    if (window.adminNavigate) window.adminNavigate("#connectors/clients");
    $("#oauth-card").scrollIntoView({ behavior: "smooth" });
  } else if (act === "reindex") {
    btn.setAttribute("aria-busy", "true");
    try {
      await api(`/api/projects/${encodeURIComponent(name)}/reindex`, { method: "POST" });
      uiNotice({ title: t("admin.projects.reindex"), body: t("admin.projects.reindex_started", { name }) });
      await refreshAdminMutation("reindex");
      fillDocCount(name);
    } catch (err) {
      uiNotice({ title: t("admin.projects.reindex_failed"), body: err.message, technicalDetail: err.technicalDetail });
    } finally {
      btn.removeAttribute("aria-busy");
    }
  } else if (act === "clear-watcher-queue") {
    await clearWatcherQueueAction(name, btn);
  } else if (act === "remove") {
    const { ok, checked } = await uiConfirm({
      title: t("admin.projects.remove.title"),
      body: t("admin.projects.remove.body", { name }),
      checkboxLabel: t("admin.projects.remove.index_data"),
      confirmLabel: t("admin.action.remove"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/api/projects/${encodeURIComponent(name)}?deleteData=${checked}`, { method: "DELETE" });
      await refreshAdminMutation("project:delete");
    } catch (err) {
      uiNotice({ title: t("admin.projects.remove_failed"), body: err.message, technicalDetail: err.technicalDetail });
    }
  }
});

$("#refresh-btn").addEventListener("click", loadProjects);

// ---- Authentication policy editor ----------------------------------------
// Authentication is rendered from the shared cache. Drafts and accordion
// state are the only local state and never contain credentials.
const authState = { revision: 0, global: null, projects: [], warnings: {}, drafts: new Map(), expanded: new Set(), search: "", oneTimeKey: "", invokingControl: null, busy: new Set() };

function authKeyLabel(record) { return record && record.configured ? t("admin.authentication.key_configured", { key_id: record.key_id, created: when(record.created_at) }) : t("admin.authentication.key_none"); }
function authStatusLabel(project) {
  if (project.effective_oauth_enabled && project.effective_static_key_id) return t("admin.authentication.oauth_plus_key");
  if (project.effective_oauth_enabled) return t("admin.authentication.oauth_only");
  if (project.effective_static_key_id) return t("admin.authentication.key_only");
  return t("admin.authentication.key_none");
}
function projectDraft(name, project) {
  const existing = authState.drafts.get(name);
  if (existing) return existing;
  return { oauth_mode: project.oauth_mode };
}
function globalDraft() {
  if (!authState.drafts.has("__global__")) authState.drafts.set("__global__", { oauth_enabled: authState.global.oauth_enabled });
  return authState.drafts.get("__global__");
}
function authDirty(name = null) { return authState.drafts.has(name || "__global__"); }
function projectSummary(project) {
  const oauth = t(project.effective_oauth_enabled ? "admin.authentication.oauth_enabled" : "admin.authentication.oauth_disabled");
  const inherited = t(project.oauth_mode === "inherit" ? "admin.authentication.inherited" : "admin.authentication.override");
  const key = t(project.effective_static_key_source === "project" ? "admin.authentication.project_key" : project.effective_static_key_source === "global" ? "admin.authentication.inherited_global_key" : "admin.authentication.key_none");
  return `${oauth} (${inherited}) · ${key} · ${authStatusLabel(project)}${project.locked_out ? " · " + t("admin.authentication.locked_out") : ""}`;
}

function renderAuthentication(data, reset = false) {
  if (!data) return;
  authState.revision = data.revision;
  authState.global = data.global;
  authState.projects = Array.isArray(data.projects) ? data.projects : [];
  authState.warnings = data.warnings || {};
  const names = new Set(authState.projects.map((p) => p.name));
  for (const name of [...authState.expanded]) if (!names.has(name)) authState.expanded.delete(name);
  if (reset) for (const name of [...authState.drafts.keys()]) if (name !== "__global__" && !names.has(name)) authState.drafts.delete(name);
  $("#authentication-revision").textContent = t("admin.authentication.policy_revision", { revision: window.CognitaAdminLocale.number(data.revision) });
  $("#authentication-global-summary").textContent = `${t(authState.global.oauth_enabled ? "admin.authentication.oauth_enabled" : "admin.authentication.oauth_disabled")} · ${authKeyLabel(authState.global.static_key)}`;
  const gd = authState.drafts.get("__global__");
  $("#authentication-global").innerHTML = `<div class="auth-project-grid"><label>${esc(t("admin.authentication.oauth_enabled_default"))} <input id="auth-global-oauth" type="checkbox" ${((gd ? gd.oauth_enabled : authState.global.oauth_enabled) ? "checked" : "")} /></label><span><strong>${esc(t("admin.authentication.global_static_key"))}</strong> ${esc(authKeyLabel(authState.global.static_key))}</span><span class="auth-project-actions"><button type="button" class="secondary outline" data-auth-action="global-generate" ${authState.busy.has("__global__") ? "disabled aria-busy=\"true\"" : ""}>${esc(t("admin.authentication.generate_new"))}</button><button type="button" class="secondary outline" data-auth-action="global-revoke" ${!authState.global.static_key || authState.busy.has("__global__") ? "disabled" : ""}>${esc(t("admin.authentication.revoke_global"))}</button><button type="button" data-auth-action="global-save" ${gd ? "" : "disabled"}>${esc(t("admin.authentication.save_oauth"))}</button><button type="button" class="secondary outline" data-auth-action="global-undo" ${gd ? "" : "disabled"}>${esc(t("admin.authentication.undo"))}</button></span></div>`;
  const query = authState.search.trim().toLocaleLowerCase();
  const projects = authState.projects.filter((p) => !query || p.name.toLocaleLowerCase().includes(query));
  $("#authentication-projects").innerHTML = projects.length ? projects.map((project) => {
    const draft = projectDraft(project.name, project);
    const override = draft.oauth_mode !== "inherit";
    const open = authState.expanded.has(project.name);
    const key = project.effective_static_key_source === "project" ? authKeyLabel(project.static_key_override) : project.effective_static_key_source === "global" ? `${t("admin.authentication.inherited_global_key")} (${project.effective_static_key_id})` : t("admin.authentication.key_none");
    const excluded = state.projects.find((p) => p.name === project.name)?.exclude_from_default_permissions ? " · " + t("admin.authentication.exclude_defaults") : "";
    return `<details class="auth-project" data-auth-project="${esc(project.name)}" ${open ? "open" : ""}><summary><span><strong>${esc(project.name)}</strong></span><span class="auth-summary-badges">${esc(projectSummary(project))}${esc(excluded)}</span></summary><div class="auth-project-body"><div class="auth-project-grid"><label><input type="checkbox" data-auth-override ${override ? "checked" : ""}> ${esc(t("admin.authentication.override_global"))}</label><label>${esc(t("admin.authentication.allow_oauth"))} <select data-auth-oauth ${override ? "" : "disabled"}><option value="enabled" ${draft.oauth_mode === "enabled" ? "selected" : ""}>${esc(t("admin.authentication.oauth_enabled"))}</option><option value="disabled" ${draft.oauth_mode === "disabled" ? "selected" : ""}>${esc(t("admin.authentication.oauth_disabled"))}</option></select></label><span>${esc(t("admin.authentication.static_key"))} ${esc(key)}</span><span>${esc(t("admin.authentication.effective"))} <strong>${esc(authStatusLabel(project))}</strong></span><span class="auth-project-actions"><button type="button" class="secondary outline" data-auth-action="project-generate" ${authState.busy.has(project.name) ? "disabled aria-busy=\"true\"" : ""}>${esc(t("admin.authentication.generate_new"))}</button><button type="button" class="secondary outline" data-auth-action="project-revoke" ${!project.static_key_override || authState.busy.has(project.name) ? "disabled" : ""}>${esc(t("admin.authentication.revoke_project"))}</button><button type="button" data-auth-action="project-save" ${authDirty(project.name) ? "" : "disabled"}>${esc(t("admin.authentication.save_oauth"))}</button><button type="button" class="secondary outline" data-auth-action="project-undo" ${authDirty(project.name) ? "" : "disabled"}>${esc(t("admin.authentication.undo"))}</button></span></div></div></details>`;
  }).join("") : `<p class="muted">${esc(t("admin.authentication.no_matching_projects"))}</p>`;
  const route = routeForHash(window.location.hash);
  if (route.top === "authentication" && route.sub === "project") {
    const target = authState.projects.find((p) => p.name === route.project);
    if (target) {
      authState.expanded.add(target.name);
      const details = document.querySelector(`[data-auth-project="${CSS.escape(target.name)}"]`);
      if (details) { details.open = true; details.querySelector("summary").setAttribute("tabindex", "-1"); details.querySelector("summary").focus({ preventScroll: true }); }
    } else {
      $("#authentication-projects").innerHTML = '<p role="alert">' + esc(t("admin.authentication.project_missing")) + '</p>';
    }
  }
  const locked = authState.projects.filter((p) => p.locked_out).map((p) => p.name);
  const connectors = authState.warnings.locked_out_connectors || [];
  const warning = $("#authentication-warning");
  warning.hidden = !locked.length && !connectors.length;
  warning.textContent = [locked.length ? t("admin.authentication.warning.projects", { projects: locked.join(", ") }) : "", connectors.length ? t("admin.authentication.warning.connectors", { connectors: connectors.join(", ") }) : ""].filter(Boolean).join(" ");
  const repair = $("#authentication-repair");
  const orphans = data.orphaned_project_entries || [];
  repair.hidden = !orphans.length;
  repair.innerHTML = orphans.length ? `<p>${esc(t("admin.authentication.orphans", { projects: orphans.join(", ") }))}</p>${orphans.map((name) => `<button type="button" class="secondary outline" data-auth-repair="${esc(name)}">${esc(t("admin.authentication.repair", { project: name }))}</button>`).join(" ")}` : "";
}

async function loadAuthentication(cached = null, preloaded = false) {
  try { renderAuthentication(preloaded ? cached : (cached || await api("/api/authentication")), false); }
  catch (err) { $("#authentication-global").innerHTML = `<p role="alert">${esc(t("admin.credential.policy_unavailable"))}: ${esc(inlineErrorText(err))}</p>`; }
}

function showAuthKey(raw, scope, project) {
  authState.oneTimeKey = raw || "";
  $("#new-auth-key").textContent = authState.oneTimeKey;
  $("#key-dialog-scope").textContent = project ? t("admin.authentication.key_scope.project", { project }) : t("admin.authentication.key_scope.global");
  $("#key-dialog").showModal();
  $("#copy-auth-key").focus();
}
function clearAuthKey() { authState.oneTimeKey = ""; $("#new-auth-key").textContent = ""; $("#key-dialog-scope").textContent = ""; $("#key-dialog-status").textContent = ""; }
async function reloadAuth() { await refreshAdminMutation("authentication:key-generate"); }

async function changeStaticKey(scope, name, action) {
  const key = name || "__global__";
  if (authDirty(name)) { await uiNotice({ title: t("admin.authentication.unsaved.title"), body: t("admin.authentication.unsaved.body") }); return; }
  const current = scope === "global" ? authState.global.static_key : authState.projects.find((p) => p.name === name)?.static_key_override;
  if (action === "revoke") {
    const inherited = scope === "project" && authState.global.static_key
      ? t("admin.authentication.inheritance_note") : "";
    const result = await uiConfirm({
      title: scope === "global" ? t("admin.authentication.revoke_global.title") : t("admin.authentication.revoke_project.title", { project: name }),
      body: scope === "global" ? t("admin.authentication.revoke_global.body") : t("admin.authentication.revoke_project.body", { inheritance: inherited }),
      confirmLabel: t(scope === "global" ? "admin.authentication.revoke_global" : "admin.authentication.revoke_project"),
      danger: true,
    });
    if (!result.ok) return;
  }
  if (action === "generate" && current) {
    const result = await uiConfirm({ title: t("admin.authentication.replace.title"), body: t("admin.authentication.replace.body", { scope: t(scope === "global" ? "admin.authentication.scope.global" : "admin.authentication.scope.project") + (scope === "project" ? ` (${name})` : "") }), confirmLabel: t("admin.authentication.generate_new"), danger: true });
    if (!result.ok) return;
  }
  authState.busy.add(key); renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings });
  try {
    const path = scope === "global" ? "/api/authentication/global/static-key/" : `/api/authentication/projects/${encodeURIComponent(name)}/static-key/`;
    const requestBody = { expected_revision: authState.revision };
    if (action === "revoke") requestBody.confirm_lockout = false;
    const result = await api(path + action, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody) });
    if (action === "generate") { authState.revision = result.revision; authState.global = result.authentication.global; authState.projects = result.authentication.projects; authState.warnings = result.authentication.warnings || {}; renderAuthentication(result.authentication); showAuthKey(result.generated_key, scope, name); }
    else if (action === "revoke") { authState.drafts.delete(name || "__global__"); await refreshAdminMutation("authentication:key-revoke"); }
  } catch (err) {
    if (err.reason === "lockout_confirmation_required") { const warning = err.payload || {}; const confirm = await uiConfirm({ title: t("admin.authentication.lockout.title"), body: t("admin.authentication.lockout.revoke.body", { projects: (warning.affected_projects || []).join(", ") }), confirmLabel: t("admin.authentication.lockout.revoke"), danger: true }); if (confirm.ok) { try { const path = scope === "global" ? "/api/authentication/global/static-key/revoke" : `/api/authentication/projects/${encodeURIComponent(name)}/static-key/revoke`; await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: authState.revision, confirm_lockout: true }) }); await refreshAdminMutation("authentication:key-revoke"); } catch (retryErr) { await uiNotice({ title: t("admin.authentication.key_revocation_failed"), body: retryErr.message, technicalDetail: retryErr.technicalDetail }); } } }
    else if (err.reason === "revision_conflict" || err.status === 409) { await loadAuthentication(); await uiNotice({ title: t("admin.authentication.changed_elsewhere.title"), body: t("admin.authentication.changed_elsewhere.body") }); }
    else if (!err.status) { await loadAuthentication(); await uiNotice({ title: t("admin.authentication.key_uncertain.title"), body: t("admin.authentication.key_uncertain.body") }); }
    else await uiNotice({ title: t("admin.authentication.key_change_failed"), body: err.message, technicalDetail: err.technicalDetail });
  } finally { authState.busy.delete(key); renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings }); }
}

async function saveAuthentication(scope, name = null) {
  const isGlobal = scope === "global"; const draft = authState.drafts.get(name || "__global__"); if (!draft) return;
  const payload = { expected_revision: authState.revision, static_key_action: "unchanged" };
  if (isGlobal) payload.oauth_enabled = draft.oauth_enabled; else payload.oauth_mode = draft.oauth_mode;
  try {
    const previewBody = { ...payload, scope: isGlobal ? "global" : "project" }; if (!isGlobal) previewBody.project = name;
    const preview = await api("/api/authentication/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(previewBody) });
    if (preview.would_lock_out) { const result = await uiConfirm({ title: t("admin.authentication.lockout.title"), body: t("admin.authentication.lockout.save.body", { projects: (preview.affected_projects || []).join(", ") }), confirmLabel: t("admin.authentication.lockout.save"), danger: true }); if (!result.ok) return; payload.confirm_lockout = true; }
    const path = isGlobal ? "/api/authentication/global" : `/api/authentication/projects/${encodeURIComponent(name)}`;
    await api(path, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); authState.drafts.delete(name || "__global__"); await refreshAdminMutation("authentication:save");
  } catch (err) { if (err.reason === "revision_conflict" || err.status === 409) { authState.drafts.delete(name || "__global__"); await loadAuthentication(); } await uiNotice({ title: t("admin.authentication.policy_change_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
}

$("#authentication-card").addEventListener("toggle", (event) => { const details = event.target.closest("details[data-auth-project]"); if (details) { if (details.open) authState.expanded.add(details.dataset.authProject); else authState.expanded.delete(details.dataset.authProject); } }, true);
$("#authentication-card").addEventListener("click", async (event) => {
  const summary = event.target.closest("summary"); const details = summary && summary.closest("details[data-auth-project]");
  if (!details || !details.open || !authDirty(details.dataset.authProject)) return;
  event.preventDefault();
  const result = await uiConfirm({ title: t("admin.authentication.discard.title"), body: t("admin.authentication.discard.body"), confirmLabel: t("admin.authentication.discard.confirm"), danger: true });
  if (result.ok) { authState.drafts.delete(details.dataset.authProject); details.open = false; }
});
$("#authentication-card").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-auth-action]"); if (!button) return; event.preventDefault();
  const row = button.closest("[data-auth-project]"); const name = row && row.dataset.authProject; const action = button.dataset.authAction;
  if (action === "global-generate") { authState.invokingControl = button; return changeStaticKey("global", null, "generate"); }
  if (action === "global-revoke") { authState.invokingControl = button; return changeStaticKey("global", null, "revoke"); }
  if (action === "global-save") return saveAuthentication("global");
  if (action === "global-undo") { authState.drafts.delete("__global__"); renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings }); return; }
  if (!name) return;
  if (action === "project-generate") { authState.invokingControl = button; return changeStaticKey("project", name, "generate"); }
  if (action === "project-revoke") { authState.invokingControl = button; return changeStaticKey("project", name, "revoke"); }
  if (action === "project-save") return saveAuthentication("project", name);
  if (action === "project-undo") { authState.drafts.delete(name); renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings }); }
});
$("#authentication-card").addEventListener("change", (event) => { if (event.target.id === "auth-global-oauth") { globalDraft().oauth_enabled = event.target.checked; renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings }); return; } const row = event.target.closest("[data-auth-project]"); if (!row) return; const project = authState.projects.find((p) => p.name === row.dataset.authProject); const draft = projectDraft(row.dataset.authProject, project); authState.drafts.set(row.dataset.authProject, draft); draft.oauth_mode = row.querySelector("[data-auth-override]").checked ? row.querySelector("[data-auth-oauth]").value : "inherit"; renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings }); });
$("#workspace-search").addEventListener("input", () => { resetWorkspacePagination(); loadWorkspaces(); });
["#workspace-state", "#workspace-owner", "#workspace-pinned", "#workspace-expired", "#workspace-warning", "#workspace-sort"].forEach((selector) => $(selector)?.addEventListener("change", () => { resetWorkspacePagination(); loadWorkspaces(); }));
$("#workspace-sort-direction").addEventListener("click", (event) => { state.workspaceSortDirection = state.workspaceSortDirection === "desc" ? "asc" : "desc"; event.currentTarget.textContent = t(state.workspaceSortDirection === "desc" ? "admin.sort.descending" : "admin.sort_direction"); resetWorkspacePagination(); loadWorkspaces(); });
$("#workspace-refresh").addEventListener("click", () => { resetWorkspacePagination(); return Promise.all([loadWorkspaces(), loadWorkspaceRuntime(), loadWorkspaceSettings()]); });
$("#workspaces-body").addEventListener("change", (event) => { if (event.target.matches("[data-workspace-select]")) updateWorkspaceSelection(); });
$("#workspaces-body").addEventListener("click", async (event) => {
  const page = event.target.closest("[data-workspace-page]");
  if (page) {
    const direction = page.dataset.workspacePage;
    if (direction === "first") {
      resetWorkspacePagination();
    } else if (direction === "previous") {
      if (!state.workspaceCursorStack.length) return;
      state.workspaceCursor = state.workspaceCursorStack.pop() || null;
    } else if (direction === "next" && state.workspaceNextCursor) {
      state.workspaceCursorStack.push(state.workspaceCursor);
      state.workspaceCursor = state.workspaceNextCursor;
    } else return;
    await loadWorkspaces();
    return;
  }
  const button = event.target.closest("button[data-workspace-op]");
  if (!button) return;
  const row = button.closest("tr[data-workspace-id]");
  const record = state.workspaces.find((item) => String(item.workspace_id || item.id) === String(row.dataset.workspaceId));
  const action = button.dataset.workspaceOp;
  if (!record) return;
  if (action === "diagnostics") {
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(row.dataset.workspaceId)}/diagnostics`);
      // Keep the durable metadata visible alongside the best-effort runtime
      // probe.  A broker failure must not hide the stored owner/state/error.
      await uiNotice({ title: t("admin.workspace.diagnostics.title"), body: JSON.stringify({
        workspace: result && (result.workspace || result.metadata) || null,
        runtime: result && result.runtime || null,
      }, null, 2) });
    } catch (err) { await uiNotice({ title: t("admin.workspace.diagnostics.failed"), body: err.message, technicalDetail: err.technicalDetail }); }
    return;
  }
  if (action === "remove") {
    if (!["verified", "absent"].includes(record.path_status)) {
      await uiNotice({ title: t("admin.workspace.removal_unavailable.title"), body: t("admin.workspace.removal_unavailable.body") });
      return;
    }
    const exactPath = record.host_path || t("admin.status.unavailable");
    const lastActivity = record.last_activity_at || record.last_accessed_at ? when(record.last_activity_at || record.last_accessed_at) : t("admin.credential.target.not_reported");
    const actualBytes = formatMaybeBytes(workspaceMeasuredValue(record, "actual_bytes", "measured_allocated_bytes"));
    const apparentBytes = formatMaybeBytes(workspaceMeasuredValue(record, "apparent_bytes", "measured_apparent_bytes"));
    const measuredAt = record.measured_at ? when(record.measured_at) : t("admin.credential.target.not_reported");
    const confirmation = await uiConfirm({ title: t("admin.workspace.remove.confirm.title"), body: t("admin.workspace.remove.confirm.body", {
      id: record.workspace_id,
      credential: record.credential_label || record.credential_id || t("admin.credential.target.not_reported"),
      state: record.state || t("admin.credential.target.not_reported"),
      desired_state: record.desired_state || t("admin.credential.target.not_reported"),
      owner: record.owner_status || t("admin.credential.target.not_reported"),
      last_activity: lastActivity,
      actual: actualBytes,
      apparent: apparentBytes,
      measured_at: measuredAt,
      path_status: record.path_status,
      path: exactPath,
    }), confirmLabel: t("admin.workspace.remove.confirm.button"), danger: true });
    if (!confirmation.ok) return;
  }
  try {
    await api(`/api/workspaces/${encodeURIComponent(row.dataset.workspaceId)}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: record.revision, confirm: action === "remove", idempotency_token: crypto.randomUUID() }) });
    await refreshAdminMutation(`workspace:${action}`);
  } catch (err) { await uiNotice({ title: t("admin.workspace.operation_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});
$("#workspace-bulk-remove").addEventListener("click", async () => {
  const records = state.workspaces.filter((item) => document.querySelector(`[data-workspace-id="${CSS.escape(String(item.workspace_id || item.id))}"] [data-workspace-select]:checked`));
  if (!records.length) return;
  const expectedRevisions = Object.fromEntries(records.map((item) => [String(item.workspace_id || item.id), item.revision]));
  let preview;
  try {
    preview = await api("/api/workspaces/bulk-delete/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "remove", workspace_ids: records.map((item) => String(item.workspace_id || item.id)), expected_revisions: expectedRevisions }) });
  } catch (err) { await uiNotice({ title: t("admin.workspace.bulk.preview_failed"), body: err.message, technicalDetail: err.technicalDetail }); return; }
  const previewTargets = Array.isArray(preview && preview.targets) ? preview.targets : null;
  const selectedIds = records.map((item) => String(item.workspace_id || item.id));
  const targetIds = previewTargets && previewTargets.map((item) => item && String(item.workspace_id || item.id || ""));
  const targetSet = targetIds && new Set(targetIds);
  const previewValid = typeof (preview && preview.preview_token) === "string" && preview.preview_token.length > 0 &&
    previewTargets && previewTargets.length === selectedIds.length &&
    targetIds.every((id, index) => id && targetSet.size === targetIds.length && selectedIds.includes(id) &&
      typeof previewTargets[index].state === "string" && previewTargets[index].state.length > 0);
  if (!previewValid) {
    await uiNotice({ title: t("admin.workspace.bulk.preview_rejected"), body: t("admin.workspace.bulk.preview_rejected.body") });
    return;
  }
  const reclaimBytes = preview && preview.reclaim_estimate_bytes;
  const reclaimEstimateVerified = isVerifiedReclaimEstimate(
    preview.reclaim_estimate_status, reclaimBytes,
  );
  const estimate = reclaimEstimateVerified ? formatMaybeBytes(reclaimBytes) : t("admin.status.unavailable");
  const targetSummary = previewTargets.map((item) => {
    const actual = formatMaybeBytes(item.actual_bytes);
    const apparent = formatMaybeBytes(item.apparent_bytes);
    const lastActivity = item.last_activity_at || item.last_accessed_at ? when(item.last_activity_at || item.last_accessed_at) : t("admin.credential.target.not_reported");
    return `${item.credential_label || item.credential_id || item.workspace_id} [${item.workspace_id}] — ${t("admin.workspace.column.state")}: ${item.state}; ${t("admin.workspace.field.owner")}: ${item.owner_status || t("admin.credential.target.not_reported")}; ${t("admin.workspace.field.last_activity")}: ${lastActivity}; ${t("admin.workspace.field.actual")}: ${actual}; ${t("admin.workspace.field.apparent")}: ${apparent}`;
  }).join("\n");
  const confirmation = await uiConfirm({ title: t("admin.workspace.bulk.confirm.title"), body: t("admin.workspace.bulk.confirm.body", { targets: targetSummary, estimate }), confirmLabel: t("admin.workspace.bulk.remove.button"), danger: true });
  if (!confirmation.ok) return;
  try {
    await api("/api/workspaces/bulk-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "remove", workspace_ids: targetIds, expected_revisions: expectedRevisions, preview_token: preview.preview_token, confirm: true, idempotency_token: crypto.randomUUID() }) });
    await refreshAdminMutation("workspace:remove");
  } catch (err) { await uiNotice({ title: t("admin.workspace.bulk.remove_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});
$("#workspace-network-add-rule").addEventListener("click", () => {
  state.workspaceNetworkEditorRules.push({ domain: "", ports: [80, 443], suffix: false });
  renderWorkspaceNetworkRules();
});
$("#workspace-network-rule-list").addEventListener("input", () => syncWorkspaceNetworkRules());
$("#workspace-network-rule-list").addEventListener("change", () => syncWorkspaceNetworkRules());
$("#workspace-network-rule-list").addEventListener("click", (event) => {
  const enable = event.target.closest("[data-network-enable-legacy]");
  if (enable) {
    const row = enable.closest("[data-network-rule-index]");
    if (!row) return;
    const shape = workspaceNetworkEditorShape(state.workspaceNetworkEditorRules[Number(row.dataset.networkRuleIndex)]);
    state.workspaceNetworkLegacyOverrides.add(workspaceNetworkShapeKey(shape));
    renderWorkspaceNetworkRules();
    return;
  }
  const remove = event.target.closest("[data-network-remove]");
  if (!remove) return;
  const row = remove.closest("[data-network-rule-index]");
  if (!row) return;
  state.workspaceNetworkEditorRules.splice(Number(row.dataset.networkRuleIndex), 1);
  renderWorkspaceNetworkRules();
});
$("#workspace-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const settings = state.workspaceSettings || {};
  const networkMode = $("#workspace-network-mode").value;
  let networkRules;
  try {
    networkRules = workspaceNetworkPayload(syncWorkspaceNetworkRules());
  } catch (err) { await uiNotice({ title: t("admin.workspace.network.invalid_rules"), body: err.message, technicalDetail: err.technicalDetail }); return; }
  const body = { expected_revision: settings.revision || 0, retention_days: Number($("#workspace-retention-days").value), quota_bytes: Number($("#workspace-quota-bytes").value), idle_stop_seconds: Number($("#workspace-idle-stop").value), host_reserve_bytes: Number($("#workspace-host-reserve").value), warning_threshold_percent: Number($("#workspace-warning-threshold").value), max_running_workspaces: Number($("#workspace-max-running").value), network_mode: networkMode, network_rules: networkRules, brave_enabled: $("#workspace-brave-enabled").checked, confirm_high_trust: $("#workspace-settings-confirm").checked, idempotency_token: crypto.randomUUID() };
  const key = $("#workspace-brave-key").value; if (key) body.brave_api_key = key;
  try { await api("/api/workspace-settings", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); $("#workspace-brave-key").value = ""; $("#workspace-settings-confirm").checked = false; await refreshAdminMutation("workspace:settings"); }
  catch (err) { await uiNotice({ title: t("admin.workspace.policy_save_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});
$("#public-url-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#public-base-url");
  const button = event.submitter || event.target.querySelector("button[type=submit]");
  if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
  try {
    await api("/api/settings/public-base-url", {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ public_base_url: input.value }),
    });
    await refreshAdminMutation("public-base-url:update");
  } catch (err) {
    await uiNotice({ title: t("admin.workspace.public_url_save_failed"), body: err.message, technicalDetail: err.technicalDetail });
  } finally {
    if (button) { button.disabled = false; button.removeAttribute("aria-busy"); }
  }
});
$("#workspace-settings-preview").addEventListener("click", async () => {
  try { const networkRules = workspaceNetworkPayload(syncWorkspaceNetworkRules()); const result = await api("/api/workspace-settings/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: (state.workspaceSettings || {}).revision || 0, network_mode: $("#workspace-network-mode").value, network_rules: networkRules, brave_enabled: $("#workspace-brave-enabled").checked, confirm_high_trust: $("#workspace-settings-confirm").checked }) }); await uiNotice({ title: t("admin.workspace.policy_preview"), body: JSON.stringify(result.preview || {}, null, 2) }); }
  catch (err) { await uiNotice({ title: t("admin.workspace.policy_preview_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});
$("#workspace-brave-test").addEventListener("click", async () => { try { const result = await api("/api/workspace-settings/test-brave", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); await uiNotice({ title: t("admin.workspace.brave_test"), body: `${t(result.ok ? "admin.action.completed" : "admin.status.unavailable")}\n\n${t("admin.technical_detail")}: ${result.category}` }); } catch (err) { await uiNotice({ title: t("admin.workspace.brave_test_failed"), body: err.message, technicalDetail: err.technicalDetail }); } });
$("#gpu-knowledge-cards").addEventListener("change", (event) => { $("#gpu-knowledge-card-ids-row").hidden = event.target.value !== "specific"; });
$("#gpu-ocr-device").addEventListener("change", (event) => { $("#gpu-ocr-card-ids-row").hidden = event.target.value !== "gpu"; });
$("#gpu-acceleration-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = state.gpuAcceleration.configured || {};
  const specific = $("#gpu-knowledge-cards").value === "specific";
  const knowledgeIds = specific ? $("#gpu-knowledge-card-ids").value.split(/\s+/).map((item) => item.trim()).filter(Boolean) : [];
  const ocrDevice = $("#gpu-ocr-device").value;
  const ocrIds = ocrDevice === "gpu" ? $("#gpu-ocr-card-ids").value.split(/\s+/).map((item) => item.trim()).filter(Boolean) : [];
  const payload = { expected_revision: Number(current.revision || 0), idempotency_token: crypto.randomUUID(), knowledge: { gpu_enabled: $("#gpu-knowledge-enabled").checked, gpu_device_ids: knowledgeIds }, ocr: { device: ocrDevice, gpu_device_ids: ocrIds } };
  try { await api("/api/settings/gpu-acceleration", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); await refreshAdminMutation("gpu-acceleration:save"); }
  catch (err) { await uiNotice({ title: t("admin.gpu.save_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});
$("#gpu-verify").addEventListener("click", async () => {
  try { const result = await api("/api/settings/gpu-acceleration/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); const stateId = GPU_VERIFY_STATE_IDS[result.state]; const stateLabel = stateId ? t(stateId) : `${t("admin.technical_detail")}: ${result.state || "unknown"}`; await uiNotice({ title: t("admin.gpu.verification"), body: t("admin.gpu.verification_results", { state: stateLabel, count: Array.isArray(result.cards) ? result.cards.length : 0 }) }); await refreshAdminMutation("gpu-acceleration:verify"); }
  catch (err) { await uiNotice({ title: t("admin.gpu.verification_failed"), body: err.message, technicalDetail: err.technicalDetail }); }
});
$("#authentication-search").addEventListener("input", (event) => { authState.search = event.target.value; renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings }); });
$("#authentication-expand-all").addEventListener("click", () => { authState.projects.forEach((p) => authState.expanded.add(p.name)); renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings }); });
$("#authentication-collapse-all").addEventListener("click", async () => { const dirty = [...authState.drafts.keys()].filter((n) => n !== "__global__"); if (dirty.length || authDirty()) { const result = await uiConfirm({ title: t("admin.authentication.discard.title"), body: t("admin.authentication.discard.collapse_body"), confirmLabel: t("admin.authentication.discard.confirm"), danger: true }); if (!result.ok) return; authState.drafts.clear(); } authState.expanded.clear(); renderAuthentication({ revision: authState.revision, global: authState.global, projects: authState.projects, warnings: authState.warnings }); });
$("#authentication-repair").addEventListener("click", async (event) => { const button = event.target.closest("[data-auth-repair]"); if (!button) return; try { await api(`/api/authentication/orphans/${encodeURIComponent(button.dataset.authRepair)}/repair`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: authState.revision }) }); await refreshAdminMutation("authentication:orphan-repair"); } catch (err) { await uiNotice({ title: t("admin.authentication.repair_failed"), body: err.message, technicalDetail: err.technicalDetail }); } });
$("#copy-auth-key").addEventListener("click", async (event) => { const ok = await copyText(authState.oneTimeKey, event.target); $("#key-dialog-status").textContent = t(ok ? "admin.action.copied" : "admin.action.copy_failed"); });
$("#auth-key-done").addEventListener("click", () => $("#key-dialog").close());
$("#key-dialog").addEventListener("close", () => { clearAuthKey(); if (authState.invokingControl) { authState.invokingControl.focus(); authState.invokingControl = null; } });
window.addEventListener("beforeunload", clearAuthKey);
$("#grants-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-revoke]");
  if (!btn) return;
  await api(`/api/oauth/grants/${encodeURIComponent(btn.dataset.revoke)}`, { method: "DELETE" });
  await refreshAdminMutation("oauth:revoke");
});
$("#revoke-all-grants").addEventListener("click", async () => {
  const connector = selectedConnector();
  if (!connector) return;
  const allGrants = adminState?.oauthGrants?.data?.grants;
  const grantIds = Array.isArray(allGrants) ? allGrants
    .filter((grant) => grantConnectorId(grant) === String(connector.id))
    .map((grant) => String(grant.id)) : [];
  if (!grantIds.length) return;
  const { ok } = await uiConfirm({ title: t("admin.oauth.revoke_all_title"), body: t("admin.oauth.revoke_all_body", { connector: connector.name }), confirmLabel: t("admin.oauth.revoke_all"), danger: true });
  if (!ok) return;
  // The existing API revokes one stable connection identity at a time.  Use
  // only the IDs from the selected connector's already loaded slice so this
  // entity-scoped action cannot affect a sibling connector.
  for (const grantId of grantIds) {
    await api("/api/oauth/grants/" + encodeURIComponent(grantId), { method: "DELETE" });
  }
  await refreshAdminMutation("oauth:revoke-all");
});
$("#logout-btn").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST" }); } catch { /* ignore */ }
  window.location.href = "/";
});

initThemeSwitch();
initBootstrap();
initAdminShell();
initConnectorSubtabs();
initSession().then(async (session) => {
  if (!session) return;
  // Session validation precedes all independent reads. The shared store
  // retains successful slices when a sibling request fails and guards every
  // result with a monotonically increasing request generation.
  const settled = await preloadAdminSlices();
  const cached = (name) => adminState && adminState.slices[name].data;
  await Promise.all([
    loadProjects(cached("projects"), true),
    loadConnectors(cached("connectors"), true),
    loadPublicBaseUrl(cached("publicBaseUrl")),
    loadOAuth(adminState && {
      status: cached("oauthStatus"),
      grants: cached("oauthGrants"),
    }),
    loadAuthentication(cached("authentication"), true),
    loadWorkspaceConnectors(cached("workspaceConnectors"), true),
    loadGpuAcceleration(cached("gpuAcceleration"), true),
  ]);
  // A failed preload is intentionally rendered by the affected legacy panel;
  // future panel migrations can use settled to show a local Retry control.
  return settled;
});
