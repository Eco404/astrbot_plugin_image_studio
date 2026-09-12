(function () {
  "use strict";

  const FILTER_KEY = "image_studio.gallery.filter_defaults.v1";
  const SORT_KEY = "image_studio.gallery.sort.v1";
  const FILTER_IDS = new Set(["galleryProvider", "galleryMode", "gallerySource", "galleryEngine"]);
  let state = { sort: "created", filters: {} };
  let local = false;
  let client = null;
  let initialRead = null;
  let writes = Promise.resolve();

  function normalizeFilter(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    if (value.mode === "all") return { mode: "all" };
    if (value.mode !== "values" || !Array.isArray(value.values) || !value.values.length || !value.values.every(item => typeof item === "string")) return null;
    return { mode: "values", values: [...new Set(value.values)] };
  }

  function normalize(value) {
    const result = { sort: value?.sort === "latest_content" ? "latest_content" : "created", filters: {} };
    for (const id of FILTER_IDS) {
      const selection = normalizeFilter(value?.filters?.[id]);
      if (selection) result.filters[id] = selection;
    }
    return result;
  }

  function readLocal() {
    let filters;
    try { filters = JSON.parse(window.localStorage.getItem(FILTER_KEY) || "{}"); } catch { filters = {}; }
    return normalize({ sort: window.localStorage.getItem(SORT_KEY), filters });
  }

  // Sandboxed plugin pages have opaque origins. Use browser cookies through the
  // host bridge there, while standalone/same-origin pages retain localStorage.
  try {
    const probe = `image_studio.gallery.probe.${Date.now()}.${Math.random()}`;
    window.localStorage.setItem(probe, "1");
    local = window.localStorage.getItem(probe) === "1";
    window.localStorage.removeItem(probe);
    if (local) state = readLocal();
  } catch { local = false; }

  function bounded(promise, timeout = 8000) {
    let timer;
    return Promise.race([
      Promise.resolve(promise),
      new Promise((_, reject) => { timer = window.setTimeout(() => reject(new Error("读取或保存浏览器显示设置超时，请稍后重试。")), timeout); }),
    ]).finally(() => window.clearTimeout(timer));
  }

  function remoteClient() {
    client ||= window.AstrBotPluginPage;
    if (!client?.apiGet || !client?.apiPost) throw new Error("浏览器存储不可用，且页面通信尚未就绪。");
    return client;
  }

  function ready(bridgeClient) {
    if (bridgeClient) client = bridgeClient;
    if (initialRead) return initialRead;
    if (local) { initialRead = Promise.resolve(); return initialRead; }
    initialRead = (async () => {
      const response = await bounded(remoteClient().apiGet("gallery/preferences"), 4000);
      state = normalize(response);
    })();
    return initialRead;
  }

  function sameFilter(left, right) {
    if (left?.mode !== right?.mode) return false;
    return left?.mode === "all" || JSON.stringify([...(left?.values || [])].sort()) === JSON.stringify([...(right?.values || [])].sort());
  }

  function patchMatches(saved, patch) {
    if (patch.sort !== undefined && saved.sort !== patch.sort) return false;
    return Object.entries(patch.filters || {}).every(([id, selection]) => sameFilter(saved.filters[id], selection));
  }

  function save(patch) {
    const operation = writes.then(async () => {
      if (local) {
        const current = readLocal();
        const next = normalize({ sort: patch.sort ?? current.sort, filters: { ...current.filters, ...patch.filters } });
        if (patch.filters) window.localStorage.setItem(FILTER_KEY, JSON.stringify(next.filters));
        if (patch.sort !== undefined) window.localStorage.setItem(SORT_KEY, next.sort);
        const persisted = readLocal();
        if (!patchMatches(persisted, patch)) throw new Error("浏览器未能保存显示设置，请检查本地存储权限。");
        state = persisted;
      } else {
        // A failed initial read should not prevent an explicit later retry.
        // Await it first so a late response cannot overwrite a successful save.
        await ready().catch(() => {});
        const bridge = remoteClient();
        await bounded(bridge.apiPost("gallery/preferences", patch));
        const persisted = normalize(await bounded(bridge.apiGet("gallery/preferences")));
        if (!patchMatches(persisted, patch)) throw new Error("浏览器未能保存显示设置，请允许此站点使用 Cookie 后重试。");
        state = persisted;
      }
    });
    writes = operation.catch(() => {});
    return operation;
  }

  function getFilter(id) { return FILTER_IDS.has(id) ? normalizeFilter(state.filters[id]) : null; }
  function setFilter(id, selection) {
    const normalized = normalizeFilter(selection);
    if (!FILTER_IDS.has(id) || !normalized) return Promise.reject(new Error("至少选择一项有效筛选后才能设为默认。"));
    return save({ filters: { [id]: normalized } });
  }
  function setSort(value) {
    if (!["created", "latest_content"].includes(value)) return Promise.reject(new Error("无效的画廊排序方式。"));
    return save({ sort: value });
  }

  window.addEventListener("storage", event => {
    if (local && (!event.key || [FILTER_KEY, SORT_KEY].includes(event.key))) {
      try { state = readLocal(); } catch { /* Keep active preferences if storage is unavailable. */ }
    }
  });
  window.ImageStudioGalleryPreferences = { ready, getFilter, setFilter, getSort: () => state.sort, setSort };
})();
