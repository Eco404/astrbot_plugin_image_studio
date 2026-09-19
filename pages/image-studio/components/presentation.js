(function () {
  "use strict";

  const ENGINES = { novelai: "NovelAI", comfyui: "ComfyUI", a1111: "Stable Diffusion", openai_images: "OpenAI Images", gemini: "Gemini", custom_json: "自定义", unknown: "未知来源" };
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value || {}, key);
  const serial = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const $ = (id) => document.getElementById(id);

  function icon(name) {
    const library = window.StudioIcons;
    if (name === "Plus" && !library?.Plus) return '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
    if (name === "Pencil" && !library?.Pencil) return '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m16 3 5 5-12 12-6 1 1-6Z"/><path d="m14 5 5 5"/></svg>';
    if (name === "GripVertical" && !library?.GripVertical) return '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg>';
    return library?.[name] ? library.createElement(library[name], { width: 18, height: 18, "aria-hidden": "true", "stroke-width": 1.8 }).outerHTML : "";
  }

  function renderIcons(root = document) {
    root.querySelectorAll("[data-studio-icon]").forEach((item) => { item.innerHTML = icon(item.dataset.studioIcon); });
  }

  function modeLabel(mode) { return ({ text2img: "文生图", img2img: "图生图" })[mode] || "未知模式"; }
  function engineLabel(engine) { return engine === "nai" ? ENGINES.novelai : engine === "mixed" ? "混合来源" : ENGINES[engine] || engine || "未知来源"; }
  function engineOf(detail) { const engine = detail.generation_engine || (detail.provider_kind === "nai_direct" ? "novelai" : "unknown"); return engine === "nai" ? "novelai" : engine; }
  function setCommandLabel(id, label) { const button = $(id); const span = button.querySelector("span"); if (span) span.textContent = label; else button.textContent = label; button.setAttribute("aria-label", label); button.dataset.tooltip = label; button.dataset.tooltipOverflow = span ? ":scope > span:last-child" : ""; }


  // Shared geometry only; each view owns observers and scheduled layout work.
  function positionBalancedGrid(grid, columnsProperty) {
    if (!grid?.isConnected || !grid.getClientRects().length || !grid.clientWidth) return;
    const columns = Number(getComputedStyle(grid).getPropertyValue(columnsProperty)) === 1 ? 1 : 2;
    const rows = Array.from(grid.children);
    if (columns === 1) rows.forEach(row => { row.style.gridColumn = "1"; });
    // Measure at the final column width, independently of opening transforms.
    const sizes = rows.map(row => {
      const style = getComputedStyle(row);
      return Math.max(1, Math.ceil((parseFloat(style.height) || row.offsetHeight) + (parseFloat(style.marginBottom) || 0)));
    });
    const heights = [0, 0]; let column = 0;
    rows.forEach((row, index) => {
      const gridColumn = String(column + 1);
      const gridRow = `${heights[column] + 1} / span ${sizes[index]}`;
      if (row.style.gridColumn !== gridColumn) row.style.gridColumn = gridColumn;
      if (row.style.gridRow !== gridRow) row.style.gridRow = gridRow;
      heights[column] += sizes[index];
      if (columns === 2 && heights[column] > heights[1 - column]) column = 1 - column;
    });
  }

  window.ImageStudioPresentation = { ENGINES, own, serial, icon, renderIcons, modeLabel, engineLabel, engineOf, setCommandLabel, positionBalancedGrid };
})();
