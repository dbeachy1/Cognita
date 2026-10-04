// Cognita Admin 12.3 — framework-free state, routing, and refresh primitives.
// This file deliberately has no DOM dependency so the concurrency contracts
// can be exercised deterministically without a browser.
"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.CognitaAdminState = api;
})(typeof globalThis !== "undefined" ? globalThis : window, function () {
  const SLICE_ENDPOINTS = Object.freeze({
    projects: "/api/projects",
    connectors: "/api/connectors",
    workspaceConnectors: "/api/workspace-connectors",
    workspaces: "/api/workspaces",
    workspaceRuntime: "/api/workspaces/health",
    workspaceSettings: "/api/workspace-settings",
    oauthStatus: "/api/oauth/status",
    oauthGrants: "/api/oauth/grants",
    authentication: "/api/authentication",
    publicBaseUrl: "/api/settings/public-base-url",
    gpuAcceleration: "/api/settings/gpu-acceleration",
  });
  const SLICE_NAMES = Object.freeze(Object.keys(SLICE_ENDPOINTS));
  const MUTATION_SLICES = Object.freeze({
    "project:create": ["projects", "connectors", "authentication", "oauthStatus", "oauthGrants"],
    "project:update": ["projects", "connectors", "authentication", "oauthStatus", "oauthGrants"],
    "project:delete": ["projects", "connectors", "authentication", "oauthStatus", "oauthGrants"],
    reindex: ["projects"],
    "connector:create": ["connectors", "projects", "authentication", "oauthGrants"],
    "connector:update": ["connectors", "projects", "authentication", "oauthGrants"],
    "connector:delete": ["connectors", "projects", "authentication", "oauthGrants"],
    "connector:publish": ["connectors", "projects", "authentication", "oauthGrants"],
    "workspace-connector:create": ["workspaceConnectors"],
    "workspace-connector:update": ["workspaceConnectors"],
    "workspace-connector:delete": ["workspaceConnectors"],
    "workspace:start": ["workspaces", "workspaceRuntime"],
    "workspace:stop": ["workspaces", "workspaceRuntime"],
    "workspace:pin": ["workspaces"],
    "workspace:unpin": ["workspaces"],
    "workspace:remove": ["workspaces", "workspaceRuntime"],
    "workspace:retry": ["workspaces", "workspaceRuntime"],
    "workspace:diagnostics": ["workspaces"],
    "workspace:settings": ["workspaces", "workspaceRuntime", "workspaceSettings"],
    "credential:add": ["connectors", "workspaceConnectors"],
    "credential:rotate": ["connectors", "workspaceConnectors"],
    "credential:revoke": ["connectors", "workspaceConnectors"],
    "credential:delete": ["connectors", "workspaceConnectors"],
    "oauth:revoke": ["oauthGrants", "projects"],
    "oauth:revoke-all": ["oauthGrants", "projects"],
    "authentication:save": ["authentication", "oauthStatus"],
    "authentication:key-generate": ["authentication"],
    "authentication:key-revoke": ["authentication"],
    "authentication:orphan-repair": ["authentication"],
    "public-base-url:update": ["publicBaseUrl", "connectors", "projects", "workspaceConnectors", "oauthStatus", "oauthGrants"],
    "gpu-acceleration:save": ["gpuAcceleration"],
    "gpu-acceleration:verify": ["gpuAcceleration"],
  });
  const INVALIDATION_MATRIX = MUTATION_SLICES;
  const ROUTE_DEFAULT = Object.freeze({ top: "projects", sub: "view", project: null });
  const ROUTE_DEFAULTS = Object.freeze({
    projects: "view", connectors: "settings", authentication: null, workspaces: "runtime", settings: "gpu-acceleration",
  });

  const CONNECTOR_FUNCTIONS = Object.freeze({
    settings: "settings", clients: "clients", access: "access", transfer: "transfer",
  });
  const CONNECTOR_LEGACY_ALIASES = Object.freeze({
    "#connectors": "settings", "#connectors/setup": "settings",
    "#connectors/clients": "clients", "#connectors/access": "access",
    "#connectors/transfer": "transfer",
  });

  function newSlice() {
    return { status: "idle", data: null, error: null, loaded_at: 0,
      generation: 0, invalidated: false, refreshing: false };
  }

  function createAdminState(clock) {
    const slices = Object.fromEntries(SLICE_NAMES.map((name) => [name, newSlice()]));
    const state = {
      slices,
      session: { status: "idle", data: null, error: null },
      navigation: { ...ROUTE_DEFAULT },
    ui: { expandedProjects: new Set(), search: "", transient: null,
      selectedSurface: null, selectedCredential: null },
      clock: typeof clock === "function" ? clock : () => Date.now(),
    };
    // Named aliases keep panel code readable while `slices` remains the
    // canonical collection used by the coordinator.
    for (const name of SLICE_NAMES) state[name] = slices[name];
    return state;
  }

  function beginRequest(state, name) {
    const slice = state.slices[name];
    if (!slice) throw new Error(`unknown admin state slice: ${name}`);
    slice.generation += 1;
    slice.status = slice.data === null ? "loading" : "ready";
    slice.refreshing = slice.data !== null;
    slice.error = null;
    return slice.generation;
  }

  function resolveRequest(state, name, generation, data, now) {
    const slice = state.slices[name];
    if (!slice || generation !== slice.generation) return false;
    slice.status = "ready";
    slice.data = data;
    slice.error = null;
    slice.loaded_at = typeof now === "number" ? now : state.clock();
    slice.invalidated = false;
    slice.refreshing = false;
    return true;
  }

  function rejectRequest(state, name, generation, error) {
    const slice = state.slices[name];
    if (!slice || generation !== slice.generation) return false;
    slice.status = slice.data === null ? "error" : "ready";
    slice.error = error instanceof Error ? error : new Error(String(error || "Request failed"));
    slice.refreshing = false;
    return true;
  }

  function invalidate(state, names) {
    const changed = [];
    for (const name of names || []) {
      const slice = state.slices[name];
      if (!slice) continue;
      slice.invalidated = true;
      changed.push(name);
    }
    return changed;
  }

  function invalidateMutation(state, mutation) {
    const names = MUTATION_SLICES[mutation];
    if (!names) throw new Error(`unknown admin mutation: ${mutation}`);
    return invalidate(state, names);
  }

  function isStale(state, name, now, maxAge) {
    const slice = state.slices[name];
    if (!slice) return true;
    if (slice.invalidated || slice.status === "error") return true;
    if (slice.status !== "ready" || !slice.loaded_at) return true;
    return (typeof now === "number" ? now : state.clock()) - slice.loaded_at > (maxAge ?? 5000);
  }

  async function preload(state, fetcher) {
    const tasks = SLICE_NAMES.map(async (name) => {
      const generation = beginRequest(state, name);
      try {
        const data = await fetcher(SLICE_ENDPOINTS[name], name);
        resolveRequest(state, name, generation, data);
        return { name, status: "fulfilled", value: data };
      } catch (error) {
        rejectRequest(state, name, generation, error);
        return { name, status: "rejected", reason: error };
      }
    });
    // Promise.allSettled semantics are intentional: one unavailable slice
    // never blanks or prevents the other Admin panels from becoming usable.
    return Promise.all(tasks);
  }

  async function refresh(state, name, fetcher, options) {
    const opts = options || {};
    if (!opts.force && !isStale(state, name, opts.now, opts.maxAge)) return state.slices[name].data;
    const generation = beginRequest(state, name);
    try {
      const data = await fetcher(SLICE_ENDPOINTS[name], name);
      resolveRequest(state, name, generation, data, opts.now);
      return data;
    } catch (error) {
      rejectRequest(state, name, generation, error);
      throw error;
    }
  }

  function decodeProject(hash) {
    const encoded = hash.slice("#authentication/".length);
    if (!encoded) return null;
    try { return decodeURIComponent(encoded); } catch { return null; }
  }

  function parseConnectorRoute(value) {
    const raw = String(value || "").split("?", 1)[0];
    const alias = CONNECTOR_LEGACY_ALIASES[raw];
    if (alias) return { top: "connectors", sub: alias, connectorId: null, legacy: true };
    if (raw === "#connectors/add") return { top: "connectors", sub: "add", connectorId: null };
    if (!raw.startsWith("#connectors/")) return null;
    const parts = raw.slice("#connectors/".length).split("/");
    if (parts.length !== 2 || !parts[0] || !CONNECTOR_FUNCTIONS[parts[1]]) return null;
    let connectorId;
    try {
      connectorId = decodeURIComponent(parts[0]);
    } catch { return null; }
    // A single encoded segment is allowed; decoded separators and controls are not.
    if (!connectorId || connectorId.includes("/") || connectorId.includes("\\") || /[\x00-\x1f\x7f]/.test(connectorId)) return null;
    return { top: "connectors", sub: CONNECTOR_FUNCTIONS[parts[1]], connectorId };
  }

  function normalizeConnectorRoute(route, connectors) {
    const records = Array.isArray(connectors) ? connectors : [];
    const safe = records.find((item) => item && item.enabled) || records[0] || null;
    const requested = route && route.connectorId;
    const selected = requested && records.find((item) => String(item.id) === String(requested));
    const fn = route && CONNECTOR_FUNCTIONS[route.sub] ? route.sub : "settings";
    if (route && route.sub === "add") return { top: "connectors", sub: "add", connectorId: null };
    if (selected) return { top: "connectors", sub: fn, connectorId: selected.id };
    // Compatibility aliases intentionally retain their requested function.
    // A malformed, unknown, or deleted immutable identity must instead land
    // on the safe connector's Settings panel.
    const fallbackFunction = route && route.legacy ? fn : "settings";
    return safe
      ? { top: "connectors", sub: fallbackFunction, connectorId: safe.id }
      : { top: "connectors", sub: "add", connectorId: null };
  }

  function parseRoute(hash) {
    const rawHash = String(hash || "");
    const value = rawHash.split("?", 1)[0];
    if (value === "#projects/create") return { top: "projects", sub: "create", project: null };
    if (value === "#projects/view") return { top: "projects", sub: "view", project: null };
    const connector = rawHash.startsWith("#connectors") && rawHash.includes("?")
      ? null : parseConnectorRoute(value);
    if (connector) return connector;
    if (value === "#workspaces" || value === "#workspaces/runtime") return { top: "workspaces", sub: "runtime", project: null };
    if (value === "#workspaces/policy") return { top: "workspaces", sub: "policy", project: null };
    if (value === "#workspaces/connectors") return { top: "workspaces", sub: "connectors", project: null };
    if (value === "#authentication") return { top: "authentication", sub: null, project: null };
    if (value === "#settings" || value === "#settings/gpu-acceleration") return { top: "settings", sub: "gpu-acceleration", project: null };
    if (value === "#authentication/global") return { top: "authentication", sub: "global", project: null };
    if (value.startsWith("#authentication/")) {
      const project = decodeProject(value);
      if (project !== null) return { top: "authentication", sub: "project", project };
    }
    if (value.startsWith("#connectors/")) return { top: "connectors", sub: "settings", connectorId: null };
    if (value.startsWith("#workspaces/")) return { top: "workspaces", sub: "runtime", project: null };
    return { ...ROUTE_DEFAULT };
  }

  function routeFragment(route) {
    const r = route || ROUTE_DEFAULT;
    if (r.top === "connectors") {
      if (r.sub === "add" || !r.connectorId) return "#connectors/add";
      const fn = CONNECTOR_FUNCTIONS[r.sub] || "settings";
      return "#connectors/" + encodeURIComponent(String(r.connectorId)) + "/" + fn;
    }
    if (r.top === "workspaces") return "#workspaces/" + (["runtime", "policy", "connectors"].includes(r.sub) ? r.sub : "runtime");
    if (r.top === "authentication") {
      if (r.sub === "global") return "#authentication/global";
      if (r.sub === "project" && r.project != null) return "#authentication/" + encodeURIComponent(r.project);
      return "#authentication";
    }
    if (r.top === "settings") return "#settings/gpu-acceleration";
    return r.sub === "create" ? "#projects/create" : "#projects/view";
  }

  function mutationSlices(mutation) { return (MUTATION_SLICES[mutation] || []).slice(); }

  return Object.freeze({
    SLICE_ENDPOINTS, SLICE_NAMES, MUTATION_SLICES, INVALIDATION_MATRIX, ROUTE_DEFAULT, ROUTE_DEFAULTS,
    CONNECTOR_FUNCTIONS, CONNECTOR_LEGACY_ALIASES, parseConnectorRoute, normalizeConnectorRoute,
    createAdminState, beginRequest, resolveRequest, rejectRequest, invalidate,
    invalidateMutation, mutationSlices, isStale, preload, refresh, parseRoute,
    routeFragment,
  });
});
