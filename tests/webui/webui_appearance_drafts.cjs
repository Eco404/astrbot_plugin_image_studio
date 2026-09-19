const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(require("../support/webui_paths.cjs").pagePath("appearance.js"), "utf8");
const key = "image-studio:appearance:v1";
const plain = (value) => JSON.parse(JSON.stringify(value));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function mount({ initial, blocked = false, bridge } = {}) {
  const data = new Map(initial ? [[key, JSON.stringify(initial)]] : []);
  const handlers = new Map();
  const documentHandlers = new Map();
  const writes = [];
  const root = { dataset: {}, style: { setProperty() {} } };
  const document = {
    documentElement: root,
    readyState: "loading",
    getElementById: () => null,
    addEventListener: (name, callback) => documentHandlers.set(name, callback),
  };
  let changes = 0;
  const window = {
    AstrBotPluginPage: bridge,
    addEventListener: (name, callback) => handlers.set(name, callback),
    dispatchEvent: (event) => { if (event.type === "image-studio-appearance-change") changes++; },
  };
  const storage = {
    getItem(name) { if (blocked) throw new Error("blocked"); return data.get(name) ?? null; },
    setItem(name, value) { if (blocked) throw new Error("blocked"); writes.push(value); data.set(name, value); },
  };
  vm.runInNewContext(source, {
    document, window, localStorage: storage,
    Event: class Event { constructor(type) { this.type = type; } },
    MutationObserver: class MutationObserver { observe() {} },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    setTimeout, clearTimeout,
  });
  return {
    api: window.ImageStudioAppearance, root, writes,
    stored: () => JSON.parse(data.get(key)),
    initialize: () => {
      documentHandlers.get("DOMContentLoaded")();
      return window.ImageStudioAppearance.ready;
    },
    storage: (value) => handlers.get("storage")({ key, newValue: JSON.stringify(value) }),
    changes: () => changes,
  };
}

test("theme drafts preview immediately without persistence, then save or discard", async () => {
  const app = mount();
  await app.initialize();
  const baseline = plain(app.api.get());
  const initialWrites = app.writes.length;
  app.api.set({ preference: "dark", accentHue: 210 });
  assert.equal(app.root.dataset.theme, "dark");
  assert.equal(app.api.isDirty(), true);
  assert.equal(app.writes.length, initialWrites);
  assert.deepEqual(app.stored(), baseline);
  app.api.discard();
  assert.deepEqual(plain(app.api.get()), baseline);
  assert.equal(app.api.isDirty(), false);
  app.api.set({ preference: "dark" });
  await app.api.save();
  assert.equal(app.stored().preference, "dark");
  assert.equal(app.api.isDirty(), false);
  assert.ok(app.changes() >= 4);
});

test("cross-tab saved settings update the baseline while preserving local drafts", async () => {
  const app = mount();
  await app.initialize();
  app.storage({ ...app.api.get(), accentHue: 90 });
  assert.equal(app.api.get().accentHue, 90);
  assert.equal(app.api.isDirty(), false);
  app.api.set({ accentHue: 240 });
  app.storage({ ...app.api.get(), accentHue: 20 });
  assert.equal(app.api.get().accentHue, 240);
  assert.equal(app.api.isDirty(), true);
  app.api.discard();
  assert.equal(app.api.get().accentHue, 20);
  assert.equal(app.api.isDirty(), false);
});

test("opaque iframe saves verify storage and retain edits made during the save", async () => {
  const post = deferred();
  let saved;
  const app = mount({ blocked: true, bridge: {
    ready: async () => {},
    apiGet: async () => saved,
    apiPost: async (_path, value) => { saved = plain(value); await post.promise; },
  } });
  await app.initialize();
  app.api.set({ preference: "dark" });
  const saving = app.api.save();
  await Promise.resolve();
  await Promise.resolve();
  app.api.set({ accentHue: 240 });
  post.resolve();
  await saving;
  assert.equal(app.api.isDirty(), true);
  assert.equal(app.api.get().accentHue, 240);
  app.api.discard();
  assert.equal(app.api.get().preference, "dark");
  assert.equal(app.api.get().accentHue, 168);
  assert.equal(app.api.isDirty(), false);
});

test("failed cookie verification rejects, retains dirty state, and allows retry", async () => {
  let saved, shouldKeep = false;
  const app = mount({ blocked: true, bridge: {
    ready: async () => {},
    apiGet: async () => saved,
    apiPost: async (_path, value) => { if (shouldKeep) saved = plain(value); },
  } });
  await app.initialize();
  app.api.set({ preference: "dark" });
  await assert.rejects(app.api.save(), /浏览器未保留主题设置/);
  assert.equal(app.api.isDirty(), true);
  shouldKeep = true;
  await app.api.save();
  assert.equal(app.api.isDirty(), false);
});

test("late initialization preserves a draft and loads the baseline for discard", async () => {
  const read = deferred();
  const app = mount({ blocked: true, bridge: {
    ready: async () => {}, apiGet: () => read.promise,
  } });
  const loading = app.initialize();
  app.api.set({ accentHue: 240 });
  read.resolve({ ...app.api.defaults, preference: "dark" });
  await loading;
  assert.equal(app.api.get().accentHue, 240);
  assert.equal(app.api.isDirty(), true);
  app.api.discard();
  assert.equal(app.api.get().preference, "dark");
  assert.equal(app.api.get().accentHue, 168);
});

test("an old initialization response cannot replace a newly saved baseline", async () => {
  const initialRead = deferred();
  let reads = 0, saved;
  const app = mount({ blocked: true, bridge: {
    ready: async () => {},
    apiGet: async () => ++reads === 1 ? initialRead.promise : saved,
    apiPost: async (_path, value) => { saved = plain(value); },
  } });
  const loading = app.initialize();
  await Promise.resolve();
  app.api.set({ preference: "dark" });
  await app.api.save();
  initialRead.resolve(app.api.defaults);
  await loading;
  assert.equal(app.api.isDirty(), false);
  app.api.set({ preference: "light" });
  app.api.discard();
  assert.equal(app.api.get().preference, "dark");
});
