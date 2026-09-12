(function () {
  "use strict";

  // Source adapters supply their own summaries. The settings UI only depends
  // on this common contract, so additional galleries need no new poll loop.
  window.ImageStudioExternalSources = function (hooks) {
    const { state, apiGet, apiPost, escape, formatBytes, formatDate, showNotice, errorMessage } = hooks;
    const $ = (id) => document.getElementById(id);
    const sources = new Map([["nai", { id: "nai", name: "NAI 插件图库", enabled: false, status: "disabled" }]]);
    const scanRequests = new Set();
    let timer = 0, pending = null, disposed = false, revision = 0, refreshAgain = false;

    function isVisible() { return !disposed && state.view === "settings" && !document.hidden; }
    function running(source) { return ["enumerating", "scanning"].includes(source.status); }
    function sourceLabel(source) { return source.name || `${source.id} 图库`; }

    function statusText(source) {
      if (!source.enabled) return "已关闭";
      if (source.status === "enumerating") return "正在枚举历史文件…";
      if (source.status === "scanning") return `正在扫描：${Number(source.processed || 0)} 张 / ${Number(source.total || 0)} 张`;
      if (source.status === "complete") return `已完成：共 ${Number(source.indexed_count || 0)} 张`;
      if (source.status === "warning") return `扫描完成：共 ${Number(source.indexed_count || 0)} 张，部分文件需检查`;
      if (source.status === "unavailable") return "来源暂不可用";
      if (source.status === "error") return "扫描失败";
      return "等待扫描";
    }

    function render() {
      const root = $("externalSourcesList");
      if (!root) return;
      for (const source of sources.values()) {
        let card = Array.from(root.children).find(element => element.dataset.externalSource === source.id);
        if (!card) {
          card = document.createElement("div"); card.className = "external-source-card"; card.dataset.externalSource = source.id;
          card.innerHTML = `<div class="toggle-row"><label for="externalSource-${escape(source.id)}">扫描 ${escape(sourceLabel(source))}</label><label class="toggle-control"><input id="externalSource-${escape(source.id)}" data-external-enabled="${escape(source.id)}" type="checkbox" /><span aria-hidden="true"></span></label></div><div class="external-source-status-row"><span data-external-status role="status"></span><button class="quiet-button" type="button" data-external-scan>重新扫描</button></div><p class="field-hint" data-external-last></p><p class="inline-error" data-external-error></p>`;
          root.appendChild(card);
          const input = card.querySelector("input");
          input.checked = !!state.settings?.webui.external_sources?.[source.id]?.enabled;
          input.addEventListener("change", () => { render(); hooks.updateSettingsDirty(); });
          card.querySelector("[data-external-scan]").addEventListener("click", () => void scan(source.id));
        }
        const configured = !!state.settings?.webui.external_sources?.[source.id]?.enabled;
        const selected = card.querySelector("input").checked;
        const unsaved = selected !== configured;
        const status = card.querySelector("[data-external-status]");
        status.textContent = unsaved ? selected ? "保存全部设置后开始扫描" : "保存全部设置后关闭扫描" : statusText(source);
        status.dataset.status = source.status;
        card.querySelector("[data-external-scan]").disabled = !source.enabled || !selected || unsaved || running(source) || scanRequests.has(source.id);
        card.querySelector("[data-external-last]").textContent = source.last_scan_at ? `最近扫描：${formatDate(source.last_scan_at)}` : "";
        const errors = source.error ? [source.error] : (source.errors || []).map(error => typeof error === "string" ? error : error.message || "文件处理失败");
        card.querySelector("[data-external-error]").textContent = errors.slice(0, 3).join("；");
      }
    }

    function renderHealth() {
      const root = $("storageExternalSources");
      if (!root) return;
      const enabled = Array.from(sources.values()).filter(source => source.enabled);
      root.classList.toggle("is-hidden", !enabled.length);
      root.innerHTML = enabled.map(source => `<div class="storage-external-source"><h3>${escape(sourceLabel(source))}</h3><div class="storage-health-grid"><div><span>外部原图</span><strong>${Number(source.indexed_count || 0)} 张 · ${formatBytes(source.size_bytes || 0)}</strong></div><div><span>本地预览</span><strong>${Number(source.thumbnail_count || 0)} 张 · ${formatBytes(source.thumbnail_bytes || 0)}</strong></div></div><p class="field-hint">${escape(statusText(source))} · 原图不计入本插件历史限额</p></div>`).join("");
    }

    function ingest(items, observe = true) {
      let changed = false;
      for (const source of items || []) {
        if (!source?.id) continue;
        const previous = sources.get(source.id);
        if (previous && (previous.enabled !== source.enabled || previous.last_scan_at !== source.last_scan_at || previous.indexed_count !== source.indexed_count || running(previous) && !running(source))) changed = true;
        sources.set(source.id, { ...previous, ...source });
      }
      render(); renderHealth();
      if (observe && changed) hooks.invalidateBrowseCache();
    }

    function schedule() {
      window.clearTimeout(timer); timer = 0;
      if (!isVisible() || !Array.from(sources.values()).some(source => source.enabled)) return;
      timer = window.setTimeout(() => void refresh(), Array.from(sources.values()).some(running) ? 1500 : 10000);
    }

    async function refresh() {
      window.clearTimeout(timer); timer = 0;
      if (!isVisible()) return;
      if (pending) { refreshAgain = true; return pending; }
      const requestedRevision = revision;
      pending = (async () => {
        try { const payload = await apiGet("external/status"); if (requestedRevision !== revision) return; ingest(payload.sources); }
        catch (error) {
          if (isVisible() && requestedRevision === revision) $("externalSourcesError").textContent = errorMessage(error, "扫描状态读取失败");
          return;
        }
        $("externalSourcesError").textContent = "";
      })().finally(() => {
        pending = null;
        if (refreshAgain) { refreshAgain = false; if (isVisible()) { void refresh(); return; } }
        schedule();
      });
      return pending;
    }

    async function scan(id) {
      if (scanRequests.has(id)) return;
      scanRequests.add(id); render();
      try { await apiPost("external/scan", { source_id: id }); await refresh(); }
      catch (error) { showNotice(errorMessage(error, "启动扫描失败"), "error"); }
      finally { scanRequests.delete(id); render(); }
    }

    function settingsLoaded(preserveDraft = false) {
      revision++;
      for (const [id, source] of sources) {
        const input = Array.from($("externalSourcesList").querySelectorAll("input")).find(element => element.dataset.externalEnabled === id);
        if (input && !preserveDraft) input.checked = !!state.settings?.webui.external_sources?.[id]?.enabled;
        source.enabled = !!state.settings?.webui.external_sources?.[id]?.enabled;
      }
      render(); renderHealth();
      void refresh();
    }

    function settingsDraft(webui) {
      webui.external_sources = { ...(webui.external_sources || {}) };
      $("externalSourcesList").querySelectorAll("[data-external-enabled]").forEach(input => {
        const id = input.dataset.externalEnabled;
        webui.external_sources[id] = { ...(webui.external_sources[id] || {}), enabled: input.checked };
      });
    }

    function viewChanged() { if (isVisible()) void refresh(); else { window.clearTimeout(timer); timer = 0; } }
    document.addEventListener("visibilitychange", viewChanged);
    window.addEventListener("pagehide", () => { disposed = true; window.clearTimeout(timer); });
    window.addEventListener("pageshow", () => { disposed = false; viewChanged(); });
    render();
    return { settingsLoaded, settingsDraft, viewChanged, refresh, ingest };
  };
})();
