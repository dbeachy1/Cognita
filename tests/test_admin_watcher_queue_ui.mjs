import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const app = await readFile(new URL("../src/cognita/web/app.js", import.meta.url), "utf8");
function extract(signature) {
  const start = app.indexOf(signature);
  const end = app.indexOf("\n}", start) + 2;
  assert.ok(start >= 0 && end > start, `${signature} must be present`);
  return app.slice(start, end);
}

const availabilityContext = {};
vm.runInNewContext(extract("function watcherQueueEnabled("), availabilityContext);

test("queue clear is enabled only when project status reports a watcher", () => {
  assert.equal(availabilityContext.watcherQueueEnabled({ state: "running" }), true);
  for (const status of [null, undefined, false]) {
    assert.equal(availabilityContext.watcherQueueEnabled(status), false);
  }
});

async function runAction({ fail = false, statusAvailable = true } = {}) {
  const calls = [];
  const notices = [];
  const button = {
    disabled: false,
    attributes: new Set(),
    setAttribute(name) { this.attributes.add(name); },
    removeAttribute(name) { this.attributes.delete(name); },
  };
  const context = {
    encodeURIComponent,
    api: async (path, options) => {
      calls.push(["api", path, options]);
      if (fail) throw Object.assign(new Error("Watcher unavailable"), { status: 503 });
      return { project: "KEI", cleared_paths: 4, active_cancelled: true };
    },
    fillDocCount: async (name) => {
      calls.push(["status", name]);
      button.disabled = !statusAvailable;
    },
    uiNotice: async (notice) => { notices.push(notice); },
    t: (key, values) => {
      if (key === "admin.projects.clear_watcher_queue_result") {
        return `Cleared ${values.cleared_paths}; ${values.active_cancelled}`;
      }
      const text = {
        "admin.projects.clear_watcher_queue": "Clear watcher queue",
        "admin.projects.clear_watcher_queue_active_cancelled": "active batch cancelled",
        "admin.projects.clear_watcher_queue_no_active": "no active batch",
        "admin.projects.clear_watcher_queue_failed": "Could not clear watcher queue",
      }[key];
      return text;
    },
  };
  vm.runInNewContext(extract("async function clearWatcherQueueAction("), context);
  await context.clearWatcherQueueAction("KEI", button);
  return { calls, notices, button };
}

test("clear action posts, refreshes status, reports counts and preserves available state", async () => {
  const result = await runAction();
  assert.deepEqual(JSON.parse(JSON.stringify(result.calls)), [
    ["api", "/api/projects/KEI/watcher/clear-queue", { method: "POST" }],
    ["status", "KEI"],
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.notices)), [{ title: "Clear watcher queue", body: "Cleared 4; active batch cancelled" }]);
  assert.equal(result.button.disabled, false);
  assert.equal(result.button.attributes.has("aria-busy"), false);
});

test("clear action reports failures and leaves the button disabled when status says unavailable", async () => {
  const result = await runAction({ fail: true, statusAvailable: false });
  assert.deepEqual(JSON.parse(JSON.stringify(result.calls)), [
    ["api", "/api/projects/KEI/watcher/clear-queue", { method: "POST" }],
    ["status", "KEI"],
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.notices)), [{ title: "Could not clear watcher queue", body: "Watcher unavailable" }]);
  assert.equal(result.button.disabled, true);
  assert.equal(result.button.attributes.has("aria-busy"), false);
});

test("all locale catalogs define identical watcher queue messages and placeholders", async () => {
  const directory = new URL("../src/cognita/web/locales/", import.meta.url);
  const files = (await readdir(directory)).filter((name) => name.endsWith(".json"));
  const keys = [
    "admin.projects.clear_watcher_queue",
    "admin.projects.clear_watcher_queue_result",
    "admin.projects.clear_watcher_queue_active_cancelled",
    "admin.projects.clear_watcher_queue_no_active",
    "admin.projects.clear_watcher_queue_failed",
  ];
  const catalogs = await Promise.all(files.map(async (name) => [name, JSON.parse(await readFile(new URL(name, directory), "utf8"))]));
  for (const [name, catalog] of catalogs) {
    for (const key of keys) assert.equal(typeof catalog[key], "string", `${name} is missing ${key}`);
    assert.deepEqual(
      [...catalog[keys[1]].matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map((match) => match[1]).sort(),
      ["active_cancelled", "cleared_paths"],
      `${name} has unexpected result placeholders`,
    );
  }
  assert.match(app, /act === "clear-watcher-queue"\) \{\s*await clearWatcherQueueAction\(name, btn\)/);
});
