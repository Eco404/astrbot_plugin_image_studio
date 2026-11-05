/* Daily plugin logs. Disabled settings invalidate in-flight UI updates. */
(() => {
  "use strict";
  const categories = { request: "接口请求", generation: "图片生成", operation: "操作", system: "系统" };
  const levels = { DEBUG: "调试", INFO: "信息", WARNING: "警告", ERROR: "错误" };

  function create({ apiGet, apiPost, escape, showNotice, showConfirm }) {
    const $ = id => document.getElementById(id);
    let enabled = true, bound = false, page = 1, pages = 1, revision = 0, busy = false, entries = [];
    const pageSize = 50;
    const pageCardPitch = 52;
    let pagePickerRange = "";

    function closePagePicker(restoreFocus = false) {
      const picker = $("logsPagePicker");
      if (picker.hidden) return;
      picker.hidden = true;
      $("logsPage").setAttribute("aria-expanded", "false");
      if (restoreFocus) $("logsPage").focus({ preventScroll: true });
    }

    function renderPageCards() {
      const strip = $("logsPageCards"), track = $("logsPageTrack");
      // Retained logs can span many pages; render only the visible strip.
      const first = Math.max(0, Math.floor(strip.scrollLeft / pageCardPitch) - 4);
      const last = Math.min(pages, first + Math.ceil(strip.clientWidth / pageCardPitch) + 9);
      const range = `${first}:${last}:${page}:${pages}`;
      if (range === pagePickerRange) return;
      pagePickerRange = range;
      track.style.width = `${pages * pageCardPitch}px`;
      track.innerHTML = Array.from({ length: last - first }, (_, offset) => {
        const number = first + offset + 1;
        return `<button type="button" class="gallery-page-card" data-log-page="${number}" style="left:${(number - 1) * pageCardPitch}px" aria-label="第 ${number} 页"${number === page ? ' aria-current="page"' : ""}>${number}</button>`;
      }).join("");
    }

    function openPagePicker() {
      if (!enabled || busy || pages <= 1) return;
      window.ImageStudioSelect?.close();
      const bar = document.querySelector(".logs-floatingbar").getBoundingClientRect();
      $("logsPagePicker").style.bottom = `${window.innerHeight - bar.top + 8}px`;
      $("logsPagePicker").hidden = false;
      $("logsPage").setAttribute("aria-expanded", "true");
      const input = $("logsPageInput"), strip = $("logsPageCards");
      input.max = String(pages); input.value = String(page);
      pagePickerRange = ""; renderPageCards();
      strip.scrollLeft = Math.max(0, (page - 1) * pageCardPitch - (strip.clientWidth - pageCardPitch) / 2);
      renderPageCards();
    }

    function jumpPage(value) {
      if (!enabled || busy) return;
      const target = Math.max(1, Math.min(pages, Math.trunc(Number(value)) || 1));
      closePagePicker(true);
      if (target !== page) { page = target; void load(); }
    }

    function bindPagePicker() {
      const picker = $("logsPagePicker"), strip = $("logsPageCards");
      $("logsPage").addEventListener("click", () => picker.hidden ? openPagePicker() : closePagePicker());
      strip.addEventListener("scroll", renderPageCards, { passive: true });
      picker.addEventListener("click", event => {
        const button = event.target.closest("[data-log-page]");
        if (button) jumpPage(button.dataset.logPage);
      });
      $("logsPageJump").addEventListener("submit", event => { event.preventDefault(); if ($("logsPageInput").value) jumpPage($("logsPageInput").value); });
      picker.addEventListener("keydown", event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closePagePicker(true); return; }
        const button = event.target.closest("[data-log-page]");
        if (!button || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const current = Number(button.dataset.logPage);
        const target = Math.max(1, Math.min(pages, event.key === "Home" ? 1 : event.key === "End" ? pages : current + (event.key === "ArrowRight" ? 1 : -1)));
        strip.scrollLeft = Math.max(0, (target - 1) * pageCardPitch - (strip.clientWidth - pageCardPitch) / 2);
        renderPageCards();
        strip.querySelector(`[data-log-page="${target}"]`)?.focus({ preventScroll: true });
      });
      document.addEventListener("pointerdown", event => { if (!picker.hidden && !picker.contains(event.target) && !$("logsPage").contains(event.target)) closePagePicker(); }, { passive: true });
      document.addEventListener("keydown", event => { if (!picker.hidden && event.key === "Escape") { event.preventDefault(); closePagePicker(true); } });
      document.addEventListener("scroll", event => { if (!picker.hidden && !picker.contains(event.target)) closePagePicker(); }, { passive: true, capture: true });
      window.addEventListener("resize", () => closePagePicker(), { passive: true });
      let touchStart = null;
      $("logsView").addEventListener("touchstart", event => {
        touchStart = !picker.hidden && !picker.contains(event.target) && event.touches.length === 1 ? event.touches[0].clientY : null;
      }, { passive: true });
      $("logsView").addEventListener("touchmove", event => {
        if (touchStart !== null && event.touches.length === 1 && Math.abs(event.touches[0].clientY - touchStart) > 8) { closePagePicker(); touchStart = null; }
      }, { passive: true });
    }

    function controls() {
      if ($("logsRefresh")) $("logsRefresh").disabled = !enabled || busy;
      if ($("logsClear")) $("logsClear").disabled = !enabled || busy;
      if ($("logsPrevious")) $("logsPrevious").disabled = !enabled || busy || page <= 1;
      if ($("logsNext")) $("logsNext").disabled = !enabled || busy || page >= pages;
      $("logsPage").disabled = !enabled || busy || pages <= 1;
      if (!enabled || busy || pages <= 1) closePagePicker();
      if ($("logsList")) $("logsList").setAttribute("aria-busy", String(busy));
    }

    function setEnabled(value) {
      const next = Boolean(value);
      if (enabled !== next) revision++;
      enabled = next;
      if (!enabled) {
        busy = false; entries = []; page = pages = 1;
        if ($("logsList")) $("logsList").replaceChildren();
        if ($("logsPage")) $("logsPage").textContent = "";
      }
      if ($("logsEnabledNotice")) $("logsEnabledNotice").hidden = enabled;
      controls();
    }

    function render(payload) {
      entries = Array.isArray(payload.items) ? payload.items : [];
      pages = Math.max(1, Math.ceil((Number(payload.total) || 0) / pageSize));
      $("logsPage").textContent = `${page} / ${pages} · ${Number(payload.total) || 0} 条`;
      $("logsList").innerHTML = entries.length ? entries.map((entry, index) => {
        const level = Object.hasOwn(levels, entry.level) ? entry.level : "INFO";
        const category = categories[entry.category] || "系统";
        const when = new Date(entry.timestamp);
        const stamp = Number.isFinite(when.getTime()) ? when.toLocaleString() : String(entry.timestamp || "");
        const request = entry.category === "request" && entry.data;
        const context = request ? [entry.data.method, entry.data.url].filter(Boolean).join(" ") : "";
        return `<details class="log-entry" data-log-index="${index}"><summary><span class="log-level log-level--${level.toLowerCase()}">${escape(levels[level])}</span><span class="log-summary"><span class="log-message">${escape(entry.message || "")}</span>${context ? `<span class="log-request">${escape(context)}</span>` : ""}</span><span class="log-meta"><span>${escape(category)}</span><time>${escape(stamp)}</time></span></summary></details>`;
      }).join("") : '<div class="logs-empty">没有符合条件的日志</div>';
      controls();
    }

    async function load() {
      if (!enabled) return;
      closePagePicker();
      const requestRevision = ++revision;
      const from = $("logsDateFrom")?.value || "", to = $("logsDateTo")?.value || "";
      if (from && to && from > to) {
        busy = false; controls();
        showNotice("开始日期不能晚于结束日期。", "error"); return;
      }
      busy = true; controls();
      try {
        const payload = await apiGet("logs", {
          category: $("logsCategory")?.value || "", level: $("logsLevel")?.value || "",
          date_from: from, date_to: to, page, page_size: pageSize,
        });
        if (requestRevision !== revision || !enabled) return;
        if (payload.enabled === false) { setEnabled(false); return; }
        const lastPage = Math.max(1, Math.ceil((Number(payload.total) || 0) / pageSize));
        if (page > lastPage) { page = lastPage; return await load(); }
        render(payload);
      } catch (error) {
        if (requestRevision === revision && enabled) showNotice(error?.message || "日志加载失败。", "error");
      } finally {
        if (requestRevision === revision) { busy = false; controls(); }
      }
    }

    async function clear() {
      if (!enabled || busy) return;
      const previousRevision = revision;
      if (!await showConfirm("清空全部插件日志？此操作无法撤销。")) return;
      if (!enabled || previousRevision !== revision) return;
      const requestRevision = ++revision;
      busy = true; controls();
      try {
        await apiPost("logs/clear", { confirm: true });
        if (requestRevision !== revision || !enabled) return;
        entries = []; page = pages = 1;
        showNotice("日志已清空。", "success");
        await load();
      } catch (error) {
        if (requestRevision === revision && enabled) showNotice(error?.message || "日志清空失败。", "error");
      } finally {
        if (requestRevision === revision) { busy = false; controls(); }
      }
    }

    function bind() {
      if (bound) return;
      bound = true;
      bindPagePicker();
      if ($("logsCategory")) $("logsCategory").innerHTML = '<option value="">全部类型</option>' + Object.entries(categories).map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
      if ($("logsLevel")) $("logsLevel").innerHTML = '<option value="">全部等级</option>' + Object.entries(levels).map(([value, label]) => `<option value="${value}">${label} · ${value}</option>`).join("");
      for (const id of ["logsCategory", "logsLevel", "logsDateFrom", "logsDateTo"]) $(id)?.addEventListener("change", () => { page = 1; void load(); });
      $("logsRefresh")?.addEventListener("click", () => { void load(); });
      $("logsClear")?.addEventListener("click", () => { void clear(); });
      $("logsPrevious")?.addEventListener("click", () => jumpPage(page - 1));
      $("logsNext")?.addEventListener("click", () => jumpPage(page + 1));
      $("logsList")?.addEventListener("toggle", event => {
        const details = event.target;
        if (!enabled || !details.matches("details[data-log-index]") || !details.open || details.querySelector("pre")) return;
        const entry = entries[Number(details.dataset.logIndex)];
        if (!entry) return;
        const pre = document.createElement("pre");
        pre.className = "log-detail";
        pre.textContent = JSON.stringify(entry, null, 2);
        details.append(pre);
      }, true);
      setEnabled(enabled);
    }

    return { load, setEnabled, bind, closePagePicker };
  }

  window.ImageStudioLogs = { create };
})();
