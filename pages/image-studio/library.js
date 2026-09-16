(function () {
  "use strict";

  const $ = id => document.getElementById(id);
  const { own, serial, icon, renderIcons, modeLabel, engineLabel, engineOf, setCommandLabel } = window.ImageStudioPresentation;

  window.ImageStudioLibrary = function (hooks) {
    const { state, modal, escape, apiGet, apiPost, bridge, showNotice, errorMessage, formatDate, formatBytes, getGallerySort } = hooks;
    const { openModal, copyText } = modal;
    const options = (values, selected) => Object.entries(values).map(([value, label]) => `<option value="${escape(value)}" ${String(selected ?? "") === value ? "selected" : ""}>${escape(label)}</option>`).join("");
    const { promptStatusMarkup, comfyDetailsMarkup } = window.ImageStudioMetadataMarkup({ escape, parameterRows, deferredDetailSection });
    const importController = window.ImageStudioImports({
      escape, apiGet, apiPost, bridge, showNotice, errorMessage, formatDate, formatBytes, getGallerySort, openModal,
      getDetail: () => ({ id: state.detailId, detail: state.detailData, imageIndex: state.detailImageIndex }),
      getGalleryPage: () => state.galleryPage, updateDetailActions,
      getImageMedia: hooks.getImageMedia, cacheImageMedia: hooks.cacheImageMedia, loadImageMedia: hooks.loadImageMedia,
      loadGallery: hooks.loadGallery, openDetail: hooks.openDetail, switchView: hooks.switchView,
    });
    let detailCopies = [];
    let detailDeferred = [];
    let detailParameterGrids = [];
    let detailParameterObserver = null;
    let detailParameterFrame = 0;
    let settingsPanelFrame = 0;
    let favoritePending = false;
    let batchFavoritePending = false;
    let selectionFavoriteKey = "";
    let selectionFavoriteRevision = 0;
    let selectionFavoriteTimer = 0;
    let selectionFavoriteAction = "favorite";
    let selectionFavoriteReady = false;
    let resizeTimer = null;
    let detailTrigger = null;
    let galleryColumns = 0;
    let detailActionsReady = false;
    let selectionScrollFrame = 0;
    function schemaPolicyButton(name) {
      return `<button class="studio-icon-button parameter-copy" data-edit-schema-policy="${escape(name)}" type="button" aria-label="编辑 ${escape(name)} 的参数行为" data-tooltip="编辑参数行为">${icon("Settings2")}</button>`;
    }

    function editParameterPolicy(name, descriptor) {
      const fields = [["webui_visible", "在生图面板显示"], ["record_in_history", "保存到请求记录"], ["refill_from_history", "复现时使用历史值"]];
      const body = fields.map(([key, label]) => `<div class="toggle-row"><label for="schemaPolicy-${key}">${label}</label><label class="toggle-control"><input id="schemaPolicy-${key}" type="checkbox" ${descriptor[key] !== false ? "checked" : ""}><span aria-hidden="true"></span></label></div>`).join("");
      const syncDependencies = (changedKey) => {
        const visible = $("schemaPolicy-webui_visible");
        const recorded = $("schemaPolicy-record_in_history");
        const refill = $("schemaPolicy-refill_from_history");
        if (changedKey === "refill_from_history" && refill.checked) {
          visible.checked = true;
          recorded.checked = true;
        } else if (!visible.checked || !recorded.checked) refill.checked = false;
      };
      return openModal(`参数行为：${name}`, body, [{ label: "取消", action: () => false }, { label: "保存", primary: true, action: () => {
        syncDependencies();
        return Object.fromEntries(fields.map(([key]) => [key, $(`schemaPolicy-${key}`).checked]));
      } }], { dismissOutside: false, onOpen: () => {
        for (const [key] of fields) $(`schemaPolicy-${key}`).addEventListener("change", () => syncDependencies(key));
        syncDependencies();
      } });
    }

    function clearDetailParameterLayout() {
      detailParameterObserver?.disconnect(); detailParameterObserver = null;
      cancelAnimationFrame(detailParameterFrame); detailParameterFrame = 0;
      detailParameterGrids = [];
    }

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

    function positionDetailParameters() {
      detailParameterFrame = 0;
      for (const grid of detailParameterGrids) positionBalancedGrid(grid, "--parameter-columns");
    }

    function layoutSettingsPanels() {
      cancelAnimationFrame(settingsPanelFrame); settingsPanelFrame = 0;
      positionBalancedGrid($("settingsView").querySelector(".settings-layout"), "--settings-columns");
    }

    function scheduleSettingsPanelLayout() {
      if (!settingsPanelFrame) settingsPanelFrame = requestAnimationFrame(layoutSettingsPanels);
    }

    function bindSettingsPanelLayout() {
      const grid = $("settingsView").querySelector(".settings-layout");
      grid.classList.add("is-masonry");
      const observer = window.ResizeObserver ? new ResizeObserver(scheduleSettingsPanelLayout) : null;
      const observeCards = () => {
        observer?.disconnect(); observer?.observe(grid);
        for (const card of grid.children) observer?.observe(card, { box: "border-box" });
        scheduleSettingsPanelLayout();
      };
      observeCards();
      // Rebind only when whole cards change; their content is covered by size observation.
      new MutationObserver(observeCards).observe(grid, { childList: true });
      window.addEventListener("resize", scheduleSettingsPanelLayout, { passive: true });
      grid.addEventListener("toggle", scheduleSettingsPanelLayout, true);
    }

    function scheduleDetailParameterLayout() {
      if (detailParameterGrids.length && !detailParameterFrame) detailParameterFrame = requestAnimationFrame(positionDetailParameters);
    }

    function layoutDetailParameters() {
      clearDetailParameterLayout();
      detailParameterGrids = Array.from($("drawerBody").querySelectorAll(".detail-parameter-grid"));
      detailParameterGrids.forEach(grid => grid.classList.add("is-masonry"));
      positionDetailParameters();
      if (window.ResizeObserver) {
        detailParameterObserver = new ResizeObserver(scheduleDetailParameterLayout);
        for (const grid of detailParameterGrids) {
          detailParameterObserver.observe(grid);
          for (const row of grid.children) detailParameterObserver.observe(row);
        }
      }
    }

    function parameterRows(values, prefix = "") {
      if (!values || typeof values !== "object") return "";
      return Object.entries(values).map(([key, value]) => {
        const content = serial(value) ?? "null";
        const copyIndex = detailCopies.push(content) - 1;
        const label = prefix ? `${prefix}.${key}` : key;
        return `<div class="detail-parameter-row"><div class="detail-parameter-label"><span>${escape(label)}</span><button class="studio-icon-button parameter-copy" data-copy-field="${copyIndex}" type="button" aria-label="复制 ${escape(label)}" data-tooltip="复制 ${escape(label)}">${icon("Copy")}</button></div><pre>${escape(content)}</pre></div>`;
      }).join("");
    }

    function deferredDetailSection(className, title, content) {
      const index = detailDeferred.push(content) - 1;
      return `<details class="${className}" data-detail-deferred="${index}"><summary>${title}</summary></details>`;
    }

    function detailMetadataMarkup(detail, image) {
      detailCopies = [];
      detailDeferred = [];
      if (detail.lightweight && !image?._metadataLoaded) return `<div class="detail-block detail-metadata-loading" role="status"><p>${image?._metadataError ? "图片参数读取失败。" : "正在读取当前图片参数…"}</p>${image?._metadataError ? '<button class="quiet-button" data-detail-metadata-retry type="button">重试读取参数</button>' : ""}</div>`;
      const metadata = image?.metadata || {};
      const normalized = metadata.normalized || {};
      const effectiveRequest = image?.supplemental?.effective_request;
      const request = hooks.requestParameters(effectiveRequest ? { ...detail, parameters: effectiveRequest } : detail);
      const supplemental = image?.supplemental && Object.keys(image.supplemental).length ? image.supplemental : detail.supplemental || {};
      const imageParameters = ["import", "external"].includes(detail.source);
      const directoryExternal = detail.source === "external" && detail.external_source?.type === "directory";
      const imported = { ...(supplemental.display_parameters || {}), ...(supplemental.overrides || {}), ...(supplemental.overrides?.parameters || {}), prompt: supplemental.prompt ?? detail.original_prompt, model: supplemental.model ?? detail.model, mode: supplemental.mode ?? detail.mode };
      delete imported.parameters;
      const requestRows = imageParameters ? imported : { prompt: request.prompt, negative_prompt: request.negative_prompt, model: request.model, mode: modeLabel(request.mode), size: request.size, count: request.count, ...(request.parameters || {}) };
      // Hide empty legacy request fields only in this display snapshot. Nested
      // workflow inputs and the stored/exported request keep their exact values.
      for (const [key, value] of Object.entries(requestRows)) {
        const empty = value == null || typeof value === "string" && !value.trim() || typeof value === "object" && !Object.keys(value).length;
        if (empty || key === "_comfy_job_id") delete requestRows[key];
      }
      const displayNormalized = { ...normalized }; delete displayNormalized.parameters;
      const metadataRows = { ...displayNormalized, ...(normalized.parameters || {}) };
      const summaryStatus = metadata.format === "comfyui" ? promptStatusMarkup(normalized.prompt_status, "正向：") + promptStatusMarkup(normalized.negative_prompt_status, "反向：") : "";
      if (metadata.format === "comfyui") for (const key of ["condition_nodes", "stages", "outputs", "prompt_candidates"]) { delete metadataRows[key]; if (imageParameters) delete requestRows[key]; }
      const raw = metadata.raw || {};
      const rawMarkup = Object.keys(raw).length ? deferredDetailSection("detail-block raw-metadata", "图片原始元数据", () => Object.entries(raw).map(([name, value]) => deferredDetailSection("metadata-raw-field", escape(name), () => parameterRows({ [name]: value }))).join("")) : "";
      const generatedTitle = `图片生成参数 · ${escape(engineLabel(metadata.format))}`;
      const generated = Object.keys(metadataRows).length ? directoryExternal
        ? `<div class="detail-block generated-parameters"><h3>${generatedTitle}</h3>${summaryStatus}<div class="detail-parameter-grid">${parameterRows(metadataRows)}</div></div>`
        : deferredDetailSection("detail-block generated-parameters", generatedTitle, () => `${summaryStatus}<div class="detail-parameter-grid">${parameterRows(metadataRows)}</div>`) : "";
      const workflow = metadata.format === "comfyui" && normalized.stages?.length ? deferredDetailSection("comfy-workflow-info", `采样阶段与条件 · ${normalized.stages.length} 个阶段`, () => {
        const template = document.createElement("template"); template.innerHTML = comfyDetailsMarkup(metadata, true);
        template.content.firstElementChild.querySelector("summary").remove(); return template.content.firstElementChild.innerHTML;
      }) : "";
      const requestMarkup = directoryExternal ? "" : `<div class="detail-block"><h3>${detail.source === "external" ? "外部图片参数" : detail.source === "import" ? "导入信息" : effectiveRequest ? "实际请求" : "原始请求"}</h3><div class="detail-parameter-grid">${parameterRows(requestRows)}</div></div>`;
      return `${requestMarkup}${generated}${workflow}${rawMarkup}`;
    }

    function detailWarningsMarkup(detail, image) {
      const warnings = [...(image?.metadata?.warnings || []), ...(detail.file_state && detail.file_state !== "available" ? [`文件状态：${detail.file_state}`] : [])];
      return warnings.length ? `<div class="retention-notice detail-warnings" role="status">${warnings.map(escape).join("<br>")}</div>` : "";
    }

    function updateDetailActions(detail) {
      const footer = $("detailFooter");
      // A new drawer session must never inherit the previous record's actions.
      // While navigating an open drawer, keep its last resolved layout until
      // the selected image's metadata can replace it in one pass.
      if (!$("detailDrawer").classList.contains("is-open")) detailActionsReady = false;
      if (detail?._manifestPending) detail = null;
      const image = detail?.images?.[state.detailImageIndex];
      const metadataReady = !!detail && (!detail.lightweight || !!image?._metadataLoaded);
      if (!metadataReady) {
        footer.inert = true;
        footer.setAttribute("aria-busy", "true");
        if (detailActionsReady) return;
      }
      const providerKind = String(detail?.provider_kind || "").trim().toLowerCase();
      const providerEngine = ["nai_direct", "novelai_official", "openai_images", "gemini", "custom_json", "comfyui"].includes(providerKind) ? providerKind : "";
      const engineHint = [image?.supplemental?.generation_engine, detail?.generation_engine, providerEngine, image?.metadata?.format].map(value => String(value || "").trim().toLowerCase()).find(value => value && !["unknown", "mixed"].includes(value));
      const engine = ["nai", "nai_direct", "novelai_official"].includes(engineHint) ? "novelai" : engineHint;
      // A transferable prompt does not mean this plugin can reproduce its
      // source workflow. Keep native exports separate from workflow exports.
      // Preparation validates snapshots and can re-read original image bytes
      // when the cached metadata has no API graph. Report precise failures there.
      const comfyReproduction = engine === "comfyui";
      const supportsReproduction = metadataReady && (["novelai", "openai_images", "gemini", "custom_json"].includes(engine) || comfyReproduction);
      const formats = supportsReproduction && (engine !== "comfyui" || providerKind === "comfyui") ? { studio: "Image Studio 参数" } : {};
      if (["nai", "novelai"].includes(engine) || image?.metadata?.format === "novelai") {
        if (providerKind !== "novelai_official") formats.nai = "NAI 请求参数";
        if (providerKind !== "novelai_official" || image?.metadata?.raw?.Comment || image?.metadata?.raw?.comment) formats.novelai = "NovelAI 图片参数";
      }
      if (image?.metadata?.raw?.workflow) formats.workflow = "ComfyUI 工作流";
      if (image?.metadata?.format === "comfyui" && image?.metadata?.raw?.prompt) formats.comfy_api = "ComfyUI 执行图";
      if (engine === "a1111" || image?.metadata?.format === "a1111") formats.a1111 = "Stable Diffusion 参数";
      const formatNames = Object.keys(formats), hasFormats = formatNames.length > 0;
      const formatSelect = $("detailCopyFormat"), previousFormat = formatSelect.value;
      const selected = Object.prototype.hasOwnProperty.call(formats, previousFormat) ? previousFormat : formatNames[0] || "";
      const sameFormats = formatSelect.options.length === formatNames.length && formatNames.every((name, index) => formatSelect.options[index].value === name && formatSelect.options[index].textContent === formats[name]);
      // Preview/original image loads also refresh this footer. Preserve native
      // option nodes and the open custom menu when its formats have not changed.
      if (!sameFormats) formatSelect.innerHTML = options(formats, selected);
      else if (formatSelect.value !== selected) formatSelect.value = selected;
      formatSelect.disabled = !hasFormats || !metadataReady;
      formatSelect.closest(".copy-format-control").hidden = !hasFormats;
      $("detailCopy").hidden = !hasFormats;
      $("detailWorkflowDownload").hidden = !formats.workflow && !formats.comfy_api;
      $("detailFavorite").classList.toggle("is-favorite", !!detail?.is_favorite);
      $("detailFavorite").setAttribute("aria-pressed", String(!!detail?.is_favorite));
      $("detailFavorite").dataset.tooltip = detail?.is_favorite ? "取消收藏" : "收藏生成记录";
      $("detailFavorite").setAttribute("aria-label", $("detailFavorite").dataset.tooltip);
      const allowed = Object.fromEntries(["favorite", "delete", "download", "reference"].map(action => [action, detail?.allowed_actions?.[action] !== false && image?.allowed_actions?.[action] !== false]));
      const normalized = image?.metadata?.normalized || {};
      const hasParameters = !detail?.is_external || !!(detail.prompt || detail.model || normalized.prompt || normalized.model || Object.keys(normalized.parameters || {}).length || Object.keys(detail.parameters || {}).length);
      $("detailFavorite").disabled = favoritePending || !detail || allowed.favorite === false;
      $("detailUseReference").disabled = !image?.id || !!image.file_state && image.file_state !== "available" || allowed.reference === false;
      $("detailReproduce").hidden = !supportsReproduction;
      $("detailReproduce").disabled = !supportsReproduction || (!hasParameters && !comfyReproduction);
      setCommandLabel("detailReproduce", comfyReproduction ? "运行工作流" : "复现参数");
      $("detailDelete").disabled = !detail || !(detail.images || []).length || allowed.delete === false;
      for (const [id, action] of [["detailFavorite", "favorite"], ["detailUseReference", "reference"], ["detailDelete", "delete"]]) {
        const button = $(id);
        if (allowed[action] === false) { if (!button.dataset.allowedTitle) button.dataset.allowedTitle = button.dataset.tooltip; button.dataset.tooltip = "此外部图库未允许此操作"; }
        else if (button.dataset.allowedTitle) { button.dataset.tooltip = button.dataset.allowedTitle; delete button.dataset.allowedTitle; }
      }
      $("detailImportEdit").hidden = detail?.source !== "import" || !!detail?.is_external;
      $("detailImportEdit").disabled = importController.isEditing() || !detail || !(detail.images || []).length;
      $("detailCopy").disabled = !hasFormats || !metadataReady;
      detailActionsReady = metadataReady;
      footer.inert = !metadataReady;
      footer.setAttribute("aria-busy", String(!metadataReady));
      window.ImageStudioSelect?.refresh(formatSelect);
    }

    async function copyDetailFormat(download = false) {
      if (!state.detailData) return;
      const workflowAvailable = !!state.detailData.images?.[state.detailImageIndex]?.metadata?.raw?.workflow;
      const format = download ? ($("detailCopyFormat").value === "comfy_api" || !workflowAvailable ? "comfy_api" : "workflow") : $("detailCopyFormat").value;
      const image = state.detailData.images?.[state.detailImageIndex];
      try {
        const result = await apiGet(`gallery/parameters/${state.detailId}`, { image_id: image?.id, format });
        const content = typeof result.content === "string" ? result.content : JSON.stringify(result.content, null, 2);
        if (download) {
          const url = URL.createObjectURL(new Blob([content], { type: "application/json;charset=utf-8" }));
          const anchor = document.createElement("a"); anchor.href = url; anchor.download = result.filename || "workflow.json"; document.body.appendChild(anchor); anchor.click(); anchor.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        } else await copyText(content);
      } catch (error) { showNotice(errorMessage(error, download ? "工作流下载失败" : "参数复制失败"), "error"); }
    }

    async function toggleFavorite() {
      const detail = state.detailData; if (!detail || favoritePending) return;
      if (detail.allowed_actions?.favorite === false) return;
      const id = detail.id; const favorite = !detail.is_favorite;
      favoritePending = true; updateDetailActions(detail);
      try {
        const result = await apiPost("gallery/favorite", { generation_id: id, favorite });
        const actual = result.is_favorite ?? result.favorite ?? favorite;
        if (state.detailId === id && state.detailData) state.detailData.is_favorite = actual;
        const card = state.galleryItems.find((item) => item.id === id); if (card) card.is_favorite = actual;
        showNotice(detail.is_external ? actual ? "已收藏。" : "已取消收藏。" : actual ? "已收藏，本条生成记录的全部图片均受保留保护。" : detail.source === "import" ? "已取消收藏，导入记录仍然保留，不参与自动清理。" : "已取消收藏，24 小时后可参与自动清理。", "success");
        await hooks.loadGallery();
      } catch (error) { showNotice(errorMessage(error, "收藏状态更新失败"), "error"); }
      finally { favoritePending = false; updateDetailActions(state.detailData); }
    }

    function renderSelectionFavorite() {
      const button = $("favoriteSelectionButton");
      const remove = selectionFavoriteReady && selectionFavoriteAction === "unfavorite";
      setCommandLabel("favoriteSelectionButton", remove ? "取消收藏" : "收藏");
      button.classList.toggle("is-favorite", remove);
      button.disabled = !state.selectedIds.size || !selectionFavoriteReady || batchFavoritePending;
      button.setAttribute("aria-busy", String(batchFavoritePending));
    }

    function selectionChanged(force = false) {
      const ids = Array.from(state.selectedIds).sort(); const key = JSON.stringify(ids);
      if (!force && key === selectionFavoriteKey) return;
      selectionFavoriteKey = key; selectionFavoriteReady = false;
      const revision = ++selectionFavoriteRevision;
      window.clearTimeout(selectionFavoriteTimer); renderSelectionFavorite();
      if (!ids.length || batchFavoritePending) return;
      selectionFavoriteTimer = window.setTimeout(async () => {
        try {
          const result = await apiPost("gallery/favorite/status", { generation_ids: ids });
          if (revision !== selectionFavoriteRevision) return;
          selectionFavoriteAction = result.all_favorite ? "unfavorite" : "favorite";
          selectionFavoriteReady = true; renderSelectionFavorite();
        } catch (error) {
          if (revision === selectionFavoriteRevision) showNotice(errorMessage(error, "所选记录的收藏状态读取失败，请刷新画廊后重试。"), "error");
        }
      }, 100);
    }

    async function toggleSelectedFavorites() {
      const ids = Array.from(state.selectedIds);
      if (!ids.length || batchFavoritePending || !selectionFavoriteReady) return;
      batchFavoritePending = true; ++selectionFavoriteRevision;
      window.clearTimeout(selectionFavoriteTimer); renderSelectionFavorite();
      try {
        await hooks.checkGalleryAction(ids, "favorite");
        const result = await apiPost("gallery/favorite", { generation_ids: ids, action: "toggle" });
        const changed = new Map((result.items || []).map((item) => [item.id, item]));
        for (const item of state.galleryItems) if (changed.has(item.id)) Object.assign(item, changed.get(item.id));
        if (state.detailData && changed.has(state.detailId)) { Object.assign(state.detailData, changed.get(state.detailId)); updateDetailActions(state.detailData); }
        await hooks.loadGallery();
        const count = result.changed_ids?.length || 0;
        showNotice(result.action === "unfavorite" ? `已取消收藏 ${count} 条记录，保持当前勾选。` : `已收藏 ${count} 条记录，保持当前勾选。`, "success");
      } catch (error) { showNotice(errorMessage(error, "批量收藏操作失败"), "error"); }
      finally { batchFavoritePending = false; selectionChanged(true); }
    }

    async function deleteDetailImages() {
      const detail = state.detailData; if (!detail) return;
      if (detail.allowed_actions?.delete === false) return;
      const images = detail.images || []; if (!images.length) return;
      const selected = new Set(images.map((image) => image.id));
      const body = images.length === 1 ? "<p>永久删除这张图片及对应生成记录？</p>" : `<p>选择要从本条生成记录中删除的图片。</p><button class="quiet-button" id="deleteImagesSelectAll" type="button">取消全选</button><div class="delete-image-grid">${images.map((image, index) => `<label class="delete-image-choice"><img data-delete-preview="${escape(image.id)}" ${image.thumbnail_data_url || image.data_url ? `src="${escape(image.thumbnail_data_url || image.data_url)}"` : ""} alt="第 ${index + 1} 张图片" /><input type="checkbox" data-delete-image="${escape(image.id)}" checked /><span>第 ${index + 1} 张</span></label>`).join("")}</div>`;
      const consequence = `${detail.is_favorite ? "<p>此记录已收藏。</p>" : ""}${detail.is_external ? `<p class="external-delete-warning">包含外部资源（${escape(detail.external_source?.name || "外部图库")}）。将永久删除来源目录中的原图及已确认关联的参数文件，无法恢复。</p>` : ""}<p>删除全部成图会同时移除本条记录及其参考图关联。</p>`;
      let active = true, observer;
      await openModal("删除图片", body + consequence, [{ label: "取消", action: () => false }, { label: `删除 ${images.length} 张`, danger: true, id: "deleteImagesAccept", action: async () => {
        if (!selected.size) throw new Error("请至少选择一张图片。");
        const currentImageId = images[state.detailImageIndex]?.id;
        const remainingImages = images.filter((image) => !selected.has(image.id));
        let nextIndex = remainingImages.findIndex((image) => image.id === currentImageId);
        if (nextIndex < 0) {
          const successor = images.slice(state.detailImageIndex + 1).find((image) => !selected.has(image.id));
          nextIndex = successor ? remainingImages.findIndex((image) => image.id === successor.id) : Math.max(0, remainingImages.length - 1);
        }
        const result = await apiPost("gallery/images/delete", { generation_id: detail.id, image_ids: Array.from(selected), confirm_external: !!detail.is_external });
        await hooks.loadGallery();
        if (result.generation_deleted || Number(result.remaining) === 0) hooks.closeDetail();
        else await hooks.openDetail(detail.id, nextIndex);
        const errors = result.errors || [];
        showNotice(`已删除 ${Array.isArray(result.deleted) ? result.deleted.length : result.deleted ?? selected.size} 张图片。${errors.map(error => error.message || "部分文件未删除").join("；")}`, errors.length ? "error" : "success"); return true;
      } }], { onOpen: () => {
        const loadPreview = async (element) => {
          const image = images.find(image => image.id === element.dataset.deletePreview);
          if (!image || element.hasAttribute("src") || element.dataset.loading) return;
          element.dataset.loading = "true";
          try { const src = await hooks.ensureDetailPreview(image, () => active && element.isConnected); if (src && active && element.isConnected) element.src = src; }
          catch { element.alt += "（预览暂不可用）"; }
          finally { delete element.dataset.loading; }
        };
        if (window.IntersectionObserver) {
          observer = new IntersectionObserver(entries => { for (const entry of entries) if (entry.isIntersecting) void loadPreview(entry.target); }, { root: $("studioModalBody"), rootMargin: "100px" });
          $("studioModalBody").querySelectorAll('[data-delete-preview]:not([src])').forEach(element => observer.observe(element));
        } else $("studioModalBody").querySelectorAll('[data-delete-preview]:not([src])').forEach(element => void loadPreview(element));
        const update = () => { $("deleteImagesAccept").textContent = `删除 ${selected.size} 张`; $("deleteImagesAccept").disabled = !selected.size; if ($("deleteImagesSelectAll")) $("deleteImagesSelectAll").textContent = selected.size === images.length ? "取消全选" : "全选"; };
        $("studioModalBody").querySelectorAll("[data-delete-image]").forEach((input) => input.addEventListener("change", () => { input.checked ? selected.add(input.dataset.deleteImage) : selected.delete(input.dataset.deleteImage); update(); }));
        $("deleteImagesSelectAll")?.addEventListener("click", () => { const all = selected.size !== images.length; selected.clear(); $("studioModalBody").querySelectorAll("[data-delete-image]").forEach((input) => { input.checked = all; if (all) selected.add(input.dataset.deleteImage); }); update(); });
      } }).finally(() => { active = false; observer?.disconnect(); });
    }

    async function resolveParameters(content, modelRef, options = {}) {
      const request = { content, for_reproduction: options.forReproduction === true };
      const result = await apiPost("studio/parameters/resolve", { ...request, ...(modelRef ? { model_ref: modelRef } : {}) });
      if (result.requires_model_selection) {
        const candidates = result.candidates?.length ? result.candidates : state.models;
        if (!candidates.length) throw new Error("没有可用模型，请先在设置中添加模型。");
        return await openModal("选择目标模型", `<label class="field">目标模型<select id="parameterTargetModel"><option value="">请选择模型</option>${candidates.map((model) => `<option value="${escape(model.model_ref)}">${escape(model.name || model.id)} · ${escape(model.provider_name || model.provider_id || "")}</option>`).join("")}</select></label>${(result.warnings || []).length ? `<p class="import-warnings">${result.warnings.map(escape).join("<br>")}</p>` : ""}`, [{ label: "取消", action: () => false }, { label: "填入参数", primary: true, action: async () => {
          const target = $("parameterTargetModel").value; if (!target) throw new Error("请选择目标模型。");
          const resolved = await apiPost("studio/parameters/resolve", { ...request, model_ref: target });
          if (resolved.requires_model_selection) throw new Error("此模型无法接收当前参数，请选择其他模型。");
          applyResolved(resolved, options); return true;
        } }], { focus: "parameterTargetModel" });
      }
      applyResolved(result, options); return true;
    }

    function applyResolved(result, options = {}) {
      if (!result.draft) throw new Error("参数解析结果缺少可填写的内容。");
      const references = options.references?.length && result.draft.mode === "img2img" ? options.references : null;
      hooks.applyDraft(references ? { ...result.draft, references } : result.draft, options);
      const warnings = (result.warnings || []).filter((warning) => !references || warning !== "参数文本不包含原始参考图，请补充参考图后生成。");
      const unmapped = result.unmapped || {};
      const notice = $("parameterImportNotice");
      notice.innerHTML = `<button class="studio-icon-button" data-dismiss-parameter-notice type="button" aria-label="关闭参数提示" data-tooltip="关闭参数提示">${icon("X")}</button>${warnings.map((warning) => `<p>${escape(warning)}</p>`).join("")}${Object.keys(unmapped).length ? `<details><summary>未映射参数</summary><pre>${escape(serial(unmapped))}</pre></details>` : ""}`;
      notice.classList.toggle("is-hidden", !warnings.length && !Object.keys(unmapped).length);
      showNotice("参数已填入，尚未执行生成。", "success");
    }

    async function readClipboardParameters() {
      let content = "";
      try { content = await navigator.clipboard.readText(); } catch { /* Offer a paste field in sandboxed hosts. */ }
      if (content.trim()) {
        try { await resolveParameters(content); return; } catch (error) { showNotice(errorMessage(error, "参数读取失败"), "error"); }
      }
      const pasted = await openModal("粘贴生图参数", `<label class="field">参数内容<textarea id="pasteParametersInput" rows="12" spellcheck="false">${escape(content)}</textarea></label>`, [{ label: "取消", action: () => false }, { label: "读取参数", primary: true, action: () => { const value = $("pasteParametersInput").value; if (!value.trim()) throw new Error("请先粘贴参数内容。"); return value; } }], { focus: "pasteParametersInput" });
      if (typeof pasted === "string") try { await resolveParameters(pasted); } catch (error) { showNotice(errorMessage(error, "参数读取失败"), "error"); }
    }

    function currentColumns() {
      const style = getComputedStyle($("galleryGrid"));
      return Math.max(1, style.gridTemplateColumns.split(/\s+/).filter((part) => /px$/.test(part)).length);
    }

    function galleryPageSize() { const columns = currentColumns(); galleryColumns = columns; return Math.min(60, Math.ceil(24 / columns) * columns); }

    const pageCardPitch = 52;
    let pagePickerRange = "";
    const totalGalleryPages = () => Math.max(1, Math.ceil(state.galleryTotal / Math.max(1, state.galleryLimit)));

    function closeGalleryPagePicker(restoreFocus = false) {
      const picker = $("galleryPagePicker");
      if (picker.hidden) return;
      picker.hidden = true; $("galleryPageLabel").setAttribute("aria-expanded", "false");
      if (restoreFocus) $("galleryPageLabel").focus({ preventScroll: true });
    }

    function renderGalleryPageCards() {
      const strip = $("galleryPageCards"), track = $("galleryPageTrack"), total = totalGalleryPages();
      // A virtual horizontal strip keeps every page reachable without creating
      // thousands of buttons when an unlimited gallery grows large.
      const first = Math.max(0, Math.floor(strip.scrollLeft / pageCardPitch) - 4);
      const last = Math.min(total, first + Math.ceil(strip.clientWidth / pageCardPitch) + 9);
      const range = `${first}:${last}:${state.galleryPage}:${total}`;
      if (range === pagePickerRange) return;
      pagePickerRange = range;
      track.style.width = `${total * pageCardPitch}px`;
      track.innerHTML = Array.from({ length: last - first }, (_, offset) => {
        const index = first + offset;
        return `<button type="button" class="gallery-page-card" data-gallery-page="${index}" style="left:${index * pageCardPitch}px" aria-label="第 ${index + 1} 页"${index === state.galleryPage ? ' aria-current="page"' : ""}>${index + 1}</button>`;
      }).join("");
    }

    function openGalleryPagePicker() {
      if (totalGalleryPages() <= 1) return;
      window.ImageStudioSelect?.close();
      const bar = document.querySelector(".gallery-floatingbar").getBoundingClientRect();
      $("galleryPagePicker").style.bottom = `${window.innerHeight - bar.top + 8}px`;
      $("galleryPagePicker").hidden = false; $("galleryPageLabel").setAttribute("aria-expanded", "true");
      const input = $("galleryPageInput"), strip = $("galleryPageCards");
      input.max = String(totalGalleryPages()); input.value = String(state.galleryPage + 1);
      pagePickerRange = ""; renderGalleryPageCards();
      strip.scrollLeft = Math.max(0, state.galleryPage * pageCardPitch - (strip.clientWidth - pageCardPitch) / 2);
      renderGalleryPageCards();
    }

    function jumpGalleryPage(page) {
      const target = Math.max(0, Math.min(totalGalleryPages() - 1, Math.trunc(Number(page)) || 0));
      closeGalleryPagePicker(true);
      if (target !== state.galleryPage) void hooks.loadGallery(target);
    }

    function bindGalleryPagePicker() {
      const picker = $("galleryPagePicker"), strip = $("galleryPageCards");
      $("galleryPageLabel").addEventListener("click", () => picker.hidden ? openGalleryPagePicker() : closeGalleryPagePicker());
      strip.addEventListener("scroll", renderGalleryPageCards, { passive: true });
      picker.addEventListener("click", event => {
        const button = event.target.closest("[data-gallery-page]");
        if (button) jumpGalleryPage(button.dataset.galleryPage);
      });
      $("galleryPageJump").addEventListener("submit", event => { event.preventDefault(); if ($("galleryPageInput").value) jumpGalleryPage(Number($("galleryPageInput").value) - 1); });
      picker.addEventListener("keydown", event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeGalleryPagePicker(true); return; }
        const button = event.target.closest("[data-gallery-page]");
        if (!button || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const current = Number(button.dataset.galleryPage), total = totalGalleryPages();
        const target = Math.max(0, Math.min(total - 1, event.key === "Home" ? 0 : event.key === "End" ? total - 1 : current + (event.key === "ArrowRight" ? 1 : -1)));
        strip.scrollLeft = Math.max(0, target * pageCardPitch - (strip.clientWidth - pageCardPitch) / 2);
        renderGalleryPageCards();
        strip.querySelector(`[data-gallery-page="${target}"]`)?.focus({ preventScroll: true });
      });
      document.addEventListener("pointerdown", event => { if (!picker.hidden && !picker.contains(event.target) && !$("galleryPageLabel").contains(event.target)) closeGalleryPagePicker(); }, { passive: true });
      document.addEventListener("keydown", event => { if (!picker.hidden && event.key === "Escape") { event.preventDefault(); closeGalleryPagePicker(true); } });
      // Capture the page's scroll but let the horizontal page strip scroll freely.
      document.addEventListener("scroll", event => { if (!picker.hidden && !picker.contains(event.target)) closeGalleryPagePicker(); }, { passive: true, capture: true });
      window.addEventListener("resize", () => closeGalleryPagePicker(), { passive: true });
      let touchStart = null;
      $("galleryView").addEventListener("touchstart", event => {
        touchStart = !picker.hidden && !picker.contains(event.target) && event.touches.length === 1 ? [event.touches[0].clientX, event.touches[0].clientY] : null;
      }, { passive: true });
      $("galleryView").addEventListener("touchmove", event => {
        if (touchStart && event.touches.length === 1 && Math.abs(event.touches[0].clientY - touchStart[1]) > 8) { closeGalleryPagePicker(); touchStart = null; }
      }, { passive: true });
    }

    function renderGalleryCard(item, index = 0) {
      const warning = !!item.cleanup_warning;
      const selected = state.selectedIds.has(item.id);
      return `<article class="gallery-card ${item.is_favorite ? "is-favorite" : ""} ${warning ? "has-cleanup-warning" : ""} ${selected ? "is-selected" : ""}" data-gallery-id="${escape(item.id)}" tabindex="0" role="button" aria-label="查看 ${escape(item.model || item.provider_name || "图片")}">
        <div class="gallery-image-wrap">${item.thumbnail_data_url ? `<img src="${escape(item.thumbnail_data_url)}" alt="${escape(item.prompt_preview)}" loading="${index < Math.max(1, galleryColumns) * 2 ? "eager" : "lazy"}" decoding="async" />` : `<div class="gallery-missing-image">${icon("Image")}<span>图片不可用</span></div>`}
          <label class="gallery-selection"><input type="checkbox" data-select-id="${escape(item.id)}" aria-label="选择生成记录" ${selected ? "checked" : ""} /><span>${icon("Check")}</span></label>
          <span class="gallery-source-label${item.is_external ? " is-external" : ""}"${item.is_external ? ` data-tooltip="来自 ${escape(item.external_source?.name || "nai-image 插件图库")}" aria-label="${escape(engineLabel(item.generation_engine))}，来自 ${escape(item.external_source?.name || "nai-image 插件图库")}"` : ""}>${escape(engineLabel(item.generation_engine))}</span>${Number(item.image_count) > 1 ? `<span class="gallery-image-count" aria-label="${Number(item.image_count)} 张图片">${icon("Image")}<span>${Number(item.image_count)}</span></span>` : ""}${item.is_favorite ? `<span class="gallery-favorite" data-tooltip="已收藏" aria-label="已收藏">${icon("Star")}</span>` : ""}
        </div><div class="gallery-info"><strong>${escape(item.model || item.provider_name || engineLabel(item.generation_engine))}</strong><p>${escape(item.prompt_preview || "无提示词")}</p><div class="gallery-meta"><span>${modeLabel(item.mode)}</span><span>${formatDate(item.sort_time || item.created_at)}</span></div>${warning ? '<span class="cleanup-warning-label">清理候选</span>' : ""}${item.file_state && item.file_state !== "available" ? '<span class="cleanup-warning-label">文件需检查</span>' : ""}</div></article>`;
    }

    function galleryRendered(payload) {
      closeGalleryPagePicker();
      selectionChanged(true);
      syncFloatingBars();
    }

    function syncFloatingBars() {
      if (state.view !== "gallery" || totalGalleryPages() <= 1) closeGalleryPagePicker();
      const galleryBar = document.querySelector(".gallery-floatingbar");
      galleryBar.classList.toggle("is-hidden", $("galleryPagination").classList.contains("is-hidden"));
      $("galleryView").classList.toggle("has-selection", state.selectedIds.size > 0);
      $("galleryGrid").querySelectorAll("[data-gallery-id]").forEach((card) => card.classList.toggle("is-selected", state.selectedIds.has(card.dataset.galleryId)));
      syncSelectionHeader();
    }

    function syncSelectionHeader() {
      const title = document.querySelector(".topbar");
      const brand = document.querySelector(".sidebar");
      const mobile = window.matchMedia("(max-width: 900px)").matches;
      let brandProgress = 0;
      if (mobile) {
        const top = parseFloat(getComputedStyle(title).top) || 0;
        const gap = parseFloat(getComputedStyle(document.querySelector(".app-shell")).rowGap) || 0;
        const distance = brand.offsetHeight + gap;
        brandProgress = Math.max(0, Math.min(1, (top + distance - $("pageHeaderAnchor").getBoundingClientRect().top) / Math.max(1, distance)));
      }
      brand.style.setProperty("--brand-header-offset", "0px");
      brand.style.setProperty("--brand-header-opacity", String(1 - brandProgress));
      brand.style.setProperty("--brand-header-blur", `${brandProgress * 8}px`);
      brand.classList.toggle("is-brand-replaced", mobile);
      const active = state.view === "gallery" && !$("selectionBar").classList.contains("is-hidden");
      let progress = 0;
      if (active) {
        // Keep the covered header pinned; only the incoming bar's flow anchor drives fading.
        const top = parseFloat(getComputedStyle(title).top) || 0;
        const distance = title.offsetHeight + 10;
        progress = Math.max(0, Math.min(1, (top + distance - $("selectionAnchor").getBoundingClientRect().top) / distance));
      }
      title.style.setProperty("--selection-title-offset", "0px");
      title.style.setProperty("--selection-title-opacity", String(1 - progress));
      title.style.setProperty("--selection-title-blur", `${progress * 8}px`);
      title.classList.toggle("is-selection-replaced", active);
    }

    function scheduleSelectionHeader() {
      if (selectionScrollFrame) return;
      selectionScrollFrame = window.requestAnimationFrame(() => {
        selectionScrollFrame = 0;
        syncSelectionHeader();
      });
    }

    function bind() {
      modal.bind();
      importController.bind();
      bindSettingsPanelLayout();
      renderIcons();
      bindGalleryPagePicker();
      $("galleryGrid").addEventListener("keydown", (event) => { const card = event.target.closest("[data-gallery-id]"); if (event.target !== card) return; if (["Enter", " "].includes(event.key)) { event.preventDefault(); detailTrigger = card; void hooks.openDetail(card.dataset.galleryId); } });
      window.addEventListener("scroll", scheduleSelectionHeader, { passive: true });
      window.addEventListener("resize", scheduleSelectionHeader, { passive: true });
      if (window.ResizeObserver) {
        const observer = new ResizeObserver(scheduleSelectionHeader);
        for (const element of [document.querySelector(".sidebar"), document.querySelector(".topbar"), document.querySelector(".gallery-toolbar"), $("selectionBar")]) observer.observe(element);
      }
      for (const [view, name] of Object.entries({ generate: "Sparkles", gallery: "Image", import: "FolderInput", settings: "Settings2" })) {
        const item = document.querySelector(`.nav-item[data-view="${view}"] .nav-icon`); item.className = "nav-icon"; item.innerHTML = icon(name);
      }
      for (const [id, name, label] of [["galleryPrev", "ChevronLeft", "上一页"], ["galleryNext", "ChevronRight", "下一页"], ["exportButton", "Download", "导出"], ["selectAllButton", "CheckCheck", "全选当前页"], ["cancelSelectionButton", "X", "取消选择"], ["deleteButton", "Trash2", "删除所选记录"], ["saveSettingsButton", "Check", "保存全部设置"], ["confirmImportButton", "Upload", "确认导入"], ["cancelImportButton", "X", "取消导入"]]) {
        const button = $(id); button.innerHTML = `${icon(name)}<span>${label}</span>`; button.setAttribute("aria-label", label); button.dataset.tooltip = label; button.dataset.tooltipOverflow = ":scope > span:last-child"; button.classList.add("responsive-command");
      }
      $("favoriteSelectionButton").innerHTML = `${icon("Star")}<span>收藏</span>`;
      $("favoriteSelectionButton").classList.add("responsive-command");
      $("favoriteSelectionButton").addEventListener("click", () => void toggleSelectedFavorites());
      renderSelectionFavorite();
      $("gallerySearch").addEventListener("input", () => { $("galleryClearSearch").disabled = !$("gallerySearch").value; });
      $("galleryClearSearch").addEventListener("click", () => { $("gallerySearch").value = ""; $("galleryClearSearch").disabled = true; $("gallerySearch").focus(); void hooks.loadGallery(0); });
      $("parameterImportNotice").addEventListener("click", (event) => { if (event.target.closest("[data-dismiss-parameter-notice]")) $("parameterImportNotice").classList.add("is-hidden"); });
      const formatWrapper = document.createElement("label"); formatWrapper.className = "copy-format-picker"; formatWrapper.innerHTML = icon("FileJson");
      const formatControl = $("detailCopyFormat").closest(".studio-select") || $("detailCopyFormat");
      formatControl.before(formatWrapper); formatWrapper.appendChild(formatControl);
      $("galleryEngine").addEventListener("change", () => { hooks.clearGallerySelection(); void hooks.loadGallery(0); });
      $("galleryFavorite").addEventListener("click", () => {
        const button = $("galleryFavorite"); const selected = button.value !== "true";
        button.value = selected ? "true" : ""; button.setAttribute("aria-pressed", String(selected)); button.classList.toggle("is-active", selected);
        button.dataset.tooltip = selected ? "取消收藏筛选" : "仅查看已收藏";
        hooks.clearGallerySelection(); void hooks.loadGallery(0);
      });
      $("pasteParametersButton").addEventListener("click", () => void readClipboardParameters());
      // Inert keeps the existing appearance while metadata loads. The capture
      // guard also covers programmatic clicks and older embedded browsers.
      $("detailFooter").addEventListener("click", (event) => {
        if (!$("detailFooter").inert) return;
        event.preventDefault(); event.stopImmediatePropagation();
      }, true);
      $("detailFavorite").addEventListener("click", () => void toggleFavorite());
      $("detailCopy").addEventListener("click", () => void copyDetailFormat());
      $("detailWorkflowDownload").addEventListener("click", () => void copyDetailFormat(true));
      $("detailReproduce").addEventListener("click", () => void hooks.reproduce(state.detailId));
      $("detailUseReference").addEventListener("click", () => { const image = state.detailData?.images?.[state.detailImageIndex]; if (image?.id) void hooks.useGalleryImageAsReference(image); });
      $("detailDelete").addEventListener("click", () => void deleteDetailImages());
      $("drawerBody").addEventListener("click", (event) => { const button = event.target.closest("[data-copy-field]"); if (button) void copyText(detailCopies[Number(button.dataset.copyField)]); });
      $("drawerBody").addEventListener("toggle", (event) => {
        const section = event.target;
        if (section.open && section.hasAttribute("data-detail-deferred")) {
          const render = detailDeferred[Number(section.dataset.detailDeferred)];
          delete section.dataset.detailDeferred;
          if (render) section.insertAdjacentHTML("beforeend", render());
          layoutDetailParameters();
        } else scheduleDetailParameterLayout();
      }, true);
      window.addEventListener("resize", scheduleDetailParameterLayout, { passive: true });
      window.addEventListener("beforeunload", clearDetailParameterLayout);
      $("galleryGrid").addEventListener("click", (event) => { const card = event.target.closest("[data-gallery-id]"); if (card && !event.target.closest(".gallery-selection")) detailTrigger = card; });
      $("closeDrawer").addEventListener("click", () => detailTrigger?.focus?.({ preventScroll: true }));
      window.addEventListener("resize", () => {
        clearTimeout(resizeTimer); resizeTimer = window.setTimeout(() => {
          if (state.view !== "gallery") return;
          const columns = currentColumns(); if (columns === galleryColumns) return;
          const oldLimit = state.galleryLimit; const anchor = state.galleryPage * oldLimit;
          state.galleryLimit = galleryPageSize(); void hooks.loadGallery(Math.floor(anchor / state.galleryLimit));
        }, 180);
      });
    }

    return { bind, modeLabel, engineLabel, galleryPageSize, closeGalleryPagePicker, renderGalleryCard, galleryRendered, selectionChanged, syncFloatingBars, detailMetadataMarkup, detailWarningsMarkup, layoutDetailParameters, layoutSettingsPanels, clearDetailParameterLayout, updateDetailActions, copyText, resolveParameters, schemaPolicyButton, editParameterPolicy, setCommandLabel, openModal, modalOpen: modal.isOpen };
  };
})();
