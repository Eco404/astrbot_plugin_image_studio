(function () {
  "use strict";

  const ENGINES = { nai: "NAI", novelai: "NovelAI", comfyui: "ComfyUI", a1111: "Stable Diffusion", openai_images: "OpenAI Images", gemini: "Gemini", custom_json: "自定义", unknown: "未知来源" };
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value || {}, key);
  const serial = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const $ = (id) => document.getElementById(id);

  function icon(name) {
    const library = window.StudioIcons;
    return library?.[name] ? library.createElement(library[name], { width: 18, height: 18, "aria-hidden": "true", "stroke-width": 1.8 }).outerHTML : "";
  }

  function renderIcons(root = document) {
    root.querySelectorAll("[data-studio-icon]").forEach((item) => { item.innerHTML = icon(item.dataset.studioIcon); });
  }

  function decodeComment(value) {
    if (!Array.isArray(value)) return typeof value === "string" ? value : "";
    const bytes = Uint8Array.from(value);
    const signature = String.fromCharCode(...bytes.slice(0, 8));
    const payload = bytes.slice(8);
    if (signature.startsWith("UNICODE")) {
      const sample = payload.slice(0, 128);
      let evenZeros = 0, oddZeros = 0;
      sample.forEach((byte, index) => { if (!byte) index % 2 ? oddZeros++ : evenZeros++; });
      const littleEndian = payload[0] === 255 && payload[1] === 254 || !(payload[0] === 254 && payload[1] === 255) && oddZeros > evenZeros;
      return new TextDecoder(littleEndian ? "utf-16le" : "utf-16be").decode(payload).replace(/\0+$/, "");
    }
    return new TextDecoder(signature.startsWith("JIS") ? "shift_jis" : "utf-8").decode(payload).replace(/\0+$/, "");
  }

  async function extractMetadata(file) {
    if (!window.ExifReader) throw new Error("图片参数读取组件未加载，请刷新页面后重试。");
    const tags = await window.ExifReader.load(await file.arrayBuffer(), { expanded: true, async: true });
    const raw = {};
    // Keep textual JSON untouched so large ComfyUI seeds survive browser parsing.
    for (const [name, tag] of Object.entries(tags.pngText || {})) {
      if (typeof tag.value === "string") raw[name] = tag.value;
    }
    for (const name of ["Software", "ImageDescription", "UserComment", "DateTimeOriginal"]) {
      const tag = tags.exif?.[name];
      if (!tag) continue;
      if (name === "UserComment") raw[name] = decodeComment(tag.value);
      else raw[name] = typeof tag.value === "string" ? tag.value : Array.isArray(tag.value) ? tag.value.join("") : tag.description;
    }
    for (const [name, tag] of Object.entries(tags.xmp || {})) {
      if (["parameters", "prompt", "workflow", "Description", "Comment", "Software"].includes(name) && !own(raw, name)) raw[name] = typeof tag.value === "string" ? tag.value : tag.description;
    }
    return raw;
  }

  window.ImageStudioLibrary = function (hooks) {
    const { state, escape, apiGet, apiPost, bridge, showNotice, errorMessage, formatDate, formatBytes } = hooks;
    let imports = [];
    let importing = false;
    let importSequence = 0;
    let importGroupDraft = null;
    let modalClose = null;
    let modalPending = false;
    let detailCopies = [];
    let favoritePending = false;
    let resizeTimer = null;
    let detailTrigger = null;
    let galleryColumns = 0;
    let detailIdentity = "";
    let selectionScrollFrame = 0;

    function modeLabel(mode) { return ({ text2img: "文生图", img2img: "图生图" })[mode] || "未知模式"; }
    function engineLabel(engine) { return engine === "mixed" ? "混合来源" : ENGINES[engine] || engine || "未知来源"; }
    function engineOf(detail) { return detail.generation_engine || (detail.provider_kind === "nai_direct" ? "nai" : "unknown"); }
    function options(values, selected) { return Object.entries(values).map(([value, label]) => `<option value="${escape(value)}" ${String(selected ?? "") === value ? "selected" : ""}>${escape(label)}</option>`).join(""); }
    function setCommandLabel(id, label) { const button = $(id); const span = button.querySelector("span"); if (span) span.textContent = label; else button.textContent = label; button.setAttribute("aria-label", label); button.title = label; }

    async function copyText(content, success = "已复制到剪贴板。") {
      let copied = false;
      try { await navigator.clipboard.writeText(content); copied = true; } catch { /* The host iframe may deny clipboard permissions. */ }
      if (!copied) {
        const focus = document.activeElement;
        const input = document.createElement("textarea");
        input.value = content; input.className = "clipboard-buffer";
        (modalClose ? $("studioModal") : document.body).appendChild(input);
        input.select();
        try { copied = document.execCommand("copy"); } catch { copied = false; }
        input.remove(); focus?.focus?.({ preventScroll: true });
      }
      if (copied) showNotice(success, "success");
      else {
        await openModal("复制参数", `<label class="field">参数内容<textarea id="manualCopyValue" rows="12" readonly>${escape(content)}</textarea></label>`, [{ label: "关闭", action: () => true }]);
      }
      return copied;
    }

    function openModal(title, body, actions, options = {}) {
      if (modalClose) modalClose(false);
      const previousFocus = document.activeElement;
      $("studioModalTitle").textContent = title;
      $("studioModalBody").innerHTML = body;
      $("studioModalError").textContent = "";
      $("studioModalFooter").innerHTML = "";
      $("studioModalRoot").classList.remove("is-hidden");
      hooks.syncPageScrollLock();
      return new Promise((resolve) => {
        const close = (result) => {
          if (modalPending) return;
          modalClose = null; $("studioModalRoot").classList.add("is-hidden");
          hooks.syncPageScrollLock(); previousFocus?.focus?.({ preventScroll: true }); resolve(result);
        };
        modalClose = close;
        for (const definition of actions) {
          const button = document.createElement("button");
          button.type = "button"; button.className = definition.danger ? "danger-button" : definition.primary ? "primary-button" : "quiet-button";
          button.textContent = definition.label;
          if (definition.id) button.id = definition.id;
          button.addEventListener("click", async () => {
            if (modalPending) return;
            modalPending = true; $("studioModal").setAttribute("aria-busy", "true");
            $("studioModalFooter").querySelectorAll("button").forEach((item) => { item.disabled = true; });
            try { const result = await definition.action(); modalPending = false; if (result !== undefined) close(result); }
            catch (error) { $("studioModalError").textContent = errorMessage(error, "操作失败"); }
            finally { modalPending = false; $("studioModal").removeAttribute("aria-busy"); $("studioModalFooter").querySelectorAll("button").forEach((item) => { item.disabled = false; }); }
          });
          $("studioModalFooter").appendChild(button);
        }
        renderIcons($("studioModal"));
        options.onOpen?.();
        (options.focus ? $(options.focus) : $("studioModalFooter").querySelector("button"))?.focus();
      });
    }

    function importCard(item) {
      const data = item.fields;
      const warnings = [...(item.parsed?.warnings || []), ...(item.warning ? [item.warning] : [])];
      const disabled = importing || item.status === "reading";
      return `<article class="import-card glass" data-import-id="${item.id}">
        <div class="import-card-header"><strong title="${escape(item.file.name)}">${escape(item.file.name)}</strong><button class="studio-icon-button is-danger" data-remove-import="${item.id}" type="button" aria-label="移除 ${escape(item.file.name)}" title="移除图片" ${importing ? "disabled" : ""}>${icon("X")}</button></div>
        <div class="import-card-preview"><img src="${item.url}" alt="${escape(item.file.name)}" /></div><div class="import-file-meta">${formatBytes(item.file.size)}${item.width ? ` · ${item.width} × ${item.height}` : ""}</div>
        <fieldset class="import-card-fields" ${disabled ? "disabled" : ""}>
          <label class="field">生图来源<select data-import-field="generation_engine">${options(ENGINES, data.generation_engine)}</select></label>
          <label class="field">模型<input data-import-field="model" value="${escape(data.model)}" /></label>
          <label class="field">模式<select data-import-field="mode">${options({ unknown: "未知模式", text2img: "文生图", img2img: "图生图" }, data.mode || "unknown")}</select></label>
          <label class="field">生成时间<input data-import-field="generated_at" type="datetime-local" value="${escape(data.generated_at || "")}" /></label>
          <label class="field field-wide">正向提示词<textarea data-import-field="prompt" rows="3">${escape(data.prompt)}</textarea></label>
          <label class="field field-wide">反向提示词<textarea data-import-field="negative_prompt" rows="2">${escape(data.negative_prompt)}</textarea></label>
          <details class="field-wide advanced"><summary>补充参数</summary><textarea data-import-field="parameters" rows="5" spellcheck="false" aria-label="补充参数 JSON">${escape(data.parameters)}</textarea></details>
        </fieldset><div class="import-card-status ${item.status === "error" ? "is-error" : ""}" role="status">${escape(item.status === "reading" ? "正在识别图片参数…" : item.error || (item.parsed?.format && item.parsed.format !== "unknown" ? `已识别 ${engineLabel(item.parsed.format)}` : "未检测到生图参数，可手动填写"))}</div>${warnings.length ? `<div class="import-warnings">${warnings.map(escape).join("<br>")}</div>` : ""}</article>`;
    }

    function renderImports() {
      const focused = document.activeElement;
      const focusId = focused?.closest?.("[data-import-id]")?.dataset.importId;
      const focusField = focused?.dataset?.importField;
      const selection = focusField && typeof focused.selectionStart === "number" ? [focused.selectionStart, focused.selectionEnd] : null;
      $("importGrid").innerHTML = imports.map(importCard).join("");
      if (focusId && focusField) {
        const restored = $("importGrid").querySelector(`[data-import-id="${focusId}"] [data-import-field="${focusField}"]`);
        restored?.focus({ preventScroll: true }); if (selection && restored?.setSelectionRange) restored.setSelectionRange(...selection);
      }
      $("importDropzone").classList.toggle("is-hidden", imports.length > 0);
      $("importSummary").textContent = imports.length ? `已选择 ${imports.length} 张图片` : "尚未选择图片";
      $("confirmImportButton").disabled = importing || !imports.length || imports.some((item) => item.status === "reading");
      $("cancelImportButton").disabled = importing; $("chooseImportFiles").disabled = importing;
      $("importGroupOption").classList.toggle("is-hidden", imports.length < 2);
      $("importAsGroup").disabled = importing || imports.length < 2;
      if (imports.length < 2) $("importAsGroup").checked = false;
      setCommandLabel("confirmImportButton", importing ? "正在导入…" : "确认导入");
    }

    function removeImport(id) {
      if (importing) return;
      const item = imports.find((entry) => entry.id === id); if (!item) return;
      void discardImportGroup();
      URL.revokeObjectURL(item.url); imports = imports.filter((entry) => entry !== item); renderImports();
    }

    function clearImports() {
      if (importing) return;
      void discardImportGroup();
      imports.forEach((item) => URL.revokeObjectURL(item.url)); imports = [];
      $("importProgress").textContent = ""; renderImports();
    }

    async function inspectFile(item) {
      try {
        const image = new Image(); image.src = item.url; await image.decode(); item.width = image.naturalWidth; item.height = image.naturalHeight;
        let raw = {};
        try { raw = await extractMetadata(item.file); } catch (error) { item.warning = errorMessage(error, "无法读取嵌入参数，可手动填写"); }
        if (!imports.includes(item)) return;
        const parsed = await apiPost("imports/inspect", { metadata: raw, width: item.width, height: item.height });
        item.parsed = parsed;
        const normalized = parsed.normalized || {};
        item.fields = { generation_engine: normalized.generation_engine || (own(ENGINES, parsed.format) ? parsed.format : "unknown"), prompt: normalized.prompt ?? "", negative_prompt: normalized.negative_prompt ?? "", model: normalized.model ?? "", mode: normalized.mode || "unknown", parameters: JSON.stringify(normalized.parameters || {}, null, 2), generated_at: "" };
        item.status = "ready";
      } catch (error) { item.status = "error"; item.error = errorMessage(error, "图片参数识别失败，可手动填写后导入"); }
      if (imports.includes(item)) renderImports();
    }

    async function addImportFiles(files) {
      if (importing) return;
      const accepted = Array.from(files).filter((file) => /^image\/(png|jpeg|webp|gif)$/.test(file.type) || /\.(png|jpe?g|webp|gif)$/i.test(file.name));
      if (!accepted.length) { showNotice("请选择 PNG、JPEG、WebP 或 GIF 图片。", "error"); return; }
      await discardImportGroup();
      hooks.switchView("import");
      const added = [];
      for (const file of accepted) {
        if (imports.length >= 100) { showNotice("单次最多选择 100 张图片。", "error"); break; }
        if (file.size > 30 * 1024 * 1024) { showNotice(`${file.name} 超过 30 MB。`, "error"); continue; }
        const item = { id: `import_${Date.now().toString(36)}_${++importSequence}`, file, url: URL.createObjectURL(file), fields: { generation_engine: "unknown", prompt: "", negative_prompt: "", model: "", mode: "unknown", parameters: "{}", generated_at: "" }, status: "reading" };
        imports.push(item); added.push(item);
      }
      renderImports();
      // Bound decoder work so a large drop does not freeze a phone browser.
      for (let index = 0; index < added.length; index += 3) await Promise.all(added.slice(index, index + 3).map(inspectFile));
    }

    async function confirmImports() {
      if (importing || !imports.length || imports.some((item) => item.status === "reading")) return;
      let preparedItems;
      try {
        preparedItems = imports.map((item) => {
          let parameters;
          try { parameters = item.fields.parameters.trim() ? JSON.parse(item.fields.parameters) : {}; } catch { throw new Error(`${item.file.name} 的补充参数不是合法 JSON。`); }
          if (!parameters || typeof parameters !== "object" || Array.isArray(parameters)) throw new Error(`${item.file.name} 的补充参数必须是 JSON 对象。`);
          const checkNumbers = (value) => {
            if (typeof value === "number" && (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))) throw new Error(`${item.file.name} 包含超出网页安全范围的数值，请将大整数写成带双引号的字符串。`);
            if (value && typeof value === "object") Object.values(value).forEach(checkNumbers);
          };
          checkNumbers(parameters);
          const overrides = { ...item.fields, parameters, generated_at: item.fields.generated_at ? new Date(item.fields.generated_at).getTime() / 1000 : null };
          return { client_id: item.id, filename: item.file.name, overrides };
        });
        if ($("importAsGroup").checked) {
          const missing = preparedItems.filter((item) => !String(item.overrides.model || "").trim());
          if (missing.length) throw new Error(`作为图组导入时，请先填写这些图片的模型：${missing.map((item) => item.filename).join("、")}`);
          if (new Set(preparedItems.map((item) => String(item.overrides.model).trim())).size !== 1) throw new Error(`图组中的模型必须相同，当前模型不一致：${preparedItems.map((item) => `${item.filename}：${item.overrides.model}`).join("；")}`);
        }
      } catch (error) { showNotice(error.message, "error"); return; }
      importing = true; renderImports();
      if ($("importAsGroup").checked) {
        try { await confirmImportGroup(preparedItems); }
        catch (error) { $("importProgress").textContent = errorMessage(error, "图组导入失败，请重试"); }
        finally { importing = false; renderImports(); }
        return;
      }
      let succeeded = 0; let duplicates = 0;
      try {
        await discardImportGroup();
        const prepared = await apiPost("imports/prepare", { items: preparedItems });
        const client = await bridge(); const pending = imports.slice();
        for (let index = 0; index < pending.length; index++) {
          const item = pending[index]; const ticket = (prepared.items || []).find((entry) => entry.client_id === item.id);
          $("importProgress").textContent = `正在导入 ${index + 1} / ${pending.length}`;
          try {
            if (!ticket?.upload_endpoint) throw new Error(ticket?.error || "无法创建图片上传任务。");
            const result = await client.upload(ticket.upload_endpoint, item.file);
            succeeded++; if (result.duplicate) duplicates++;
            URL.revokeObjectURL(item.url); imports = imports.filter((entry) => entry !== item);
          } catch (error) { item.status = "error"; item.error = errorMessage(error, "导入失败，请重试"); }
          renderImports();
        }
        $("importProgress").textContent = `已导入 ${succeeded} 张${duplicates ? `，其中 ${duplicates} 张已存在` : ""}${imports.length ? `；${imports.length} 张待重试` : ""}`;
        if (succeeded) showNotice(`已导入 ${succeeded} 张图片。`, "success");
      } catch (error) { $("importProgress").textContent = errorMessage(error, "导入准备失败"); }
      finally { importing = false; renderImports(); }
    }

    async function discardImportGroup() {
      const draft = importGroupDraft; importGroupDraft = null;
      if (draft) await apiPost(draft.prepared.cancel_endpoint, {}).catch(() => {});
    }

    async function confirmImportGroup(items) {
      const signature = JSON.stringify(items);
      if (importGroupDraft?.signature !== signature) {
        await discardImportGroup();
        const prepared = await apiPost("imports/prepare", { items, as_group: true });
        importGroupDraft = { signature, prepared, uploaded: new Set() };
      }
      const draft = importGroupDraft;
      const client = await bridge();
      for (let index = 0; index < imports.length; index++) {
        const item = imports[index];
        if (draft.uploaded.has(item.id)) continue;
        const ticket = draft.prepared.items.find((entry) => entry.client_id === item.id);
        $("importProgress").textContent = `正在上传图组 ${index + 1} / ${imports.length}`;
        try {
          await client.upload(ticket.upload_endpoint, item.file);
          draft.uploaded.add(item.id); item.status = "ready"; item.error = "已上传，等待图组入库";
        } catch (error) {
          item.status = "error"; item.error = errorMessage(error, "上传失败，请重试"); renderImports();
          if (/过期|取消/.test(item.error)) await discardImportGroup();
          throw error;
        }
        renderImports();
      }
      $("importProgress").textContent = "正在保存图组…";
      try {
        await apiPost(draft.prepared.commit_endpoint, {});
      } catch (error) {
        if (/过期|取消/.test(errorMessage(error, ""))) await discardImportGroup();
        throw error;
      }
      const count = imports.length;
      imports.forEach((item) => URL.revokeObjectURL(item.url)); imports = [];
      importGroupDraft = null;
      $("importProgress").textContent = `已导入 1 个图组，共 ${count} 张图片`;
      showNotice(`已导入图组，共 ${count} 张图片。`, "success");
    }

    function parameterRows(values, prefix = "") {
      if (!values || typeof values !== "object") return "";
      return Object.entries(values).map(([key, value]) => {
        const copyIndex = detailCopies.push(serial(value) ?? "null") - 1;
        const label = prefix ? `${prefix}.${key}` : key;
        const content = serial(value) ?? "null";
        return `<div class="detail-parameter-row"><div class="detail-parameter-label"><span>${escape(label)}</span><button class="studio-icon-button parameter-copy" data-copy-field="${copyIndex}" type="button" aria-label="复制 ${escape(label)}" title="复制 ${escape(label)}">${icon("Copy")}</button></div><pre>${escape(content)}</pre></div>`;
      }).join("");
    }

    function detailMetadataMarkup(detail, image) {
      detailCopies = [];
      const metadata = image?.metadata || {};
      const normalized = metadata.normalized || {};
      const request = hooks.requestParameters(detail);
      const supplemental = image?.supplemental && Object.keys(image.supplemental).length ? image.supplemental : detail.supplemental || {};
      const imported = { ...(supplemental.display_parameters || {}), ...(supplemental.overrides || {}), ...(supplemental.overrides?.parameters || {}), prompt: supplemental.prompt ?? detail.original_prompt, model: supplemental.model ?? detail.model, mode: supplemental.mode ?? detail.mode };
      delete imported.parameters;
      const requestRows = detail.source === "import" ? imported : { prompt: request.prompt, negative_prompt: request.negative_prompt, model: request.model, mode: modeLabel(request.mode), size: request.size, count: request.count, ...(request.parameters || {}) };
      const displayNormalized = { ...normalized }; delete displayNormalized.parameters;
      const metadataRows = { ...displayNormalized, ...(normalized.parameters || {}) };
      const warnings = [...(metadata.warnings || []), ...(detail.file_state && detail.file_state !== "available" ? [`文件状态：${detail.file_state}`] : [])];
      const raw = metadata.raw || {};
      const rawMarkup = Object.entries(raw).map(([name, value]) => `<details class="metadata-raw-field"><summary>${escape(name)}</summary>${parameterRows({ [name]: value })}</details>`).join("");
      return `${warnings.length ? `<div class="retention-notice">${warnings.map(escape).join("<br>")}</div>` : ""}<div class="detail-block"><h3>${detail.source === "import" ? "导入信息" : "原始请求"}</h3><div class="detail-parameter-grid">${parameterRows(requestRows)}</div></div>${Object.keys(metadataRows).length ? `<div class="detail-block"><h3>图片生成参数 · ${escape(engineLabel(metadata.format))}</h3><div class="detail-parameter-grid">${parameterRows(metadataRows)}</div></div>` : ""}${rawMarkup ? `<details class="detail-block raw-metadata"><summary>图片原始元数据</summary>${rawMarkup}</details>` : ""}`;
    }

    function updateDetailActions(detail) {
      const identity = `${detail?.id || ""}:${state.detailImageIndex}`;
      const changed = detailIdentity !== identity; detailIdentity = identity;
      const image = detail?.images?.[state.detailImageIndex];
      const engine = image?.supplemental?.generation_engine || engineOf(detail || {});
      const formats = { studio: "Image Studio 参数" };
      if (["nai", "novelai"].includes(engine) || image?.metadata?.format === "novelai") { formats.nai = "NAI 请求参数"; formats.novelai = "NovelAI 图片参数"; }
      if (image?.metadata?.raw?.workflow) formats.workflow = "ComfyUI 工作流";
      if (image?.metadata?.format === "comfyui" && image?.metadata?.raw?.prompt) formats.comfy_api = "ComfyUI 执行图";
      if (engine === "a1111" || image?.metadata?.format === "a1111") formats.a1111 = "Stable Diffusion 参数";
      const selected = changed ? "studio" : $("detailCopyFormat").value;
      $("detailCopyFormat").innerHTML = options(formats, selected);
      $("detailWorkflowDownload").hidden = !formats.workflow && !formats.comfy_api;
      $("detailFavorite").classList.toggle("is-favorite", !!detail?.is_favorite);
      $("detailFavorite").setAttribute("aria-pressed", String(!!detail?.is_favorite));
      $("detailFavorite").title = detail?.is_favorite ? "取消收藏" : "收藏生成记录";
      $("detailFavorite").setAttribute("aria-label", $("detailFavorite").title);
      $("detailFavorite").disabled = favoritePending || !detail;
      $("detailUseReference").disabled = !state.detailAssetsLoaded || !image?.data_url;
      $("detailReproduce").disabled = !detail;
      $("detailDelete").disabled = !detail || !(detail.images || []).length;
      $("detailCopy").disabled = !detail;
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
      const id = detail.id; const favorite = !detail.is_favorite;
      favoritePending = true; updateDetailActions(detail);
      try {
        const result = await apiPost("gallery/favorite", { generation_id: id, favorite });
        const actual = result.is_favorite ?? result.favorite ?? favorite;
        if (state.detailId === id && state.detailData) state.detailData.is_favorite = actual;
        const card = state.galleryItems.find((item) => item.id === id); if (card) card.is_favorite = actual;
        showNotice(actual ? "已收藏，本条生成记录的全部图片均受保留保护。" : detail.source === "import" ? "已取消收藏，导入记录仍然保留，不参与自动清理。" : "已取消收藏，24 小时后可参与自动清理。", "success");
        await hooks.loadGallery();
      } catch (error) { showNotice(errorMessage(error, "收藏状态更新失败"), "error"); }
      finally { favoritePending = false; updateDetailActions(state.detailData); }
    }

    async function deleteDetailImages() {
      const detail = state.detailData; if (!detail) return;
      const images = detail.images || []; if (!images.length) return;
      const selected = new Set(images.map((image) => image.id));
      const body = images.length === 1 ? "<p>永久删除这张图片及对应生成记录？</p>" : `<p>选择要从本条生成记录中删除的图片。</p><button class="quiet-button" id="deleteImagesSelectAll" type="button">取消全选</button><div class="delete-image-grid">${images.map((image, index) => `<label class="delete-image-choice"><img src="${escape(image.thumbnail_data_url || image.data_url || "")}" alt="第 ${index + 1} 张图片" /><input type="checkbox" data-delete-image="${escape(image.id)}" checked /><span>第 ${index + 1} 张</span></label>`).join("")}</div>`;
      const consequence = `${detail.is_favorite ? "<p>此记录已收藏。</p>" : ""}<p>删除全部成图会同时移除本条记录及其参考图关联。</p>`;
      await openModal("删除图片", body + consequence, [{ label: "取消", action: () => false }, { label: `删除 ${images.length} 张`, danger: true, id: "deleteImagesAccept", action: async () => {
        if (!selected.size) throw new Error("请至少选择一张图片。");
        const currentImageId = images[state.detailImageIndex]?.id;
        const remainingImages = images.filter((image) => !selected.has(image.id));
        let nextIndex = remainingImages.findIndex((image) => image.id === currentImageId);
        if (nextIndex < 0) {
          const successor = images.slice(state.detailImageIndex + 1).find((image) => !selected.has(image.id));
          nextIndex = successor ? remainingImages.findIndex((image) => image.id === successor.id) : Math.max(0, remainingImages.length - 1);
        }
        const result = await apiPost("gallery/images/delete", { generation_id: detail.id, image_ids: Array.from(selected) });
        await hooks.loadGallery();
        if (result.generation_deleted || Number(result.remaining) === 0) hooks.closeDetail();
        else await hooks.openDetail(detail.id, nextIndex);
        showNotice(`已删除 ${Array.isArray(result.deleted) ? result.deleted.length : result.deleted ?? selected.size} 张图片。`, "success"); return true;
      } }], { onOpen: () => {
        const update = () => { $("deleteImagesAccept").textContent = `删除 ${selected.size} 张`; $("deleteImagesAccept").disabled = !selected.size; if ($("deleteImagesSelectAll")) $("deleteImagesSelectAll").textContent = selected.size === images.length ? "取消全选" : "全选"; };
        $("studioModalBody").querySelectorAll("[data-delete-image]").forEach((input) => input.addEventListener("change", () => { input.checked ? selected.add(input.dataset.deleteImage) : selected.delete(input.dataset.deleteImage); update(); }));
        $("deleteImagesSelectAll")?.addEventListener("click", () => { const all = selected.size !== images.length; selected.clear(); $("studioModalBody").querySelectorAll("[data-delete-image]").forEach((input) => { input.checked = all; if (all) selected.add(input.dataset.deleteImage); }); update(); });
      } });
    }

    async function resolveParameters(content, modelRef) {
      const result = await apiPost("studio/parameters/resolve", { content, ...(modelRef ? { model_ref: modelRef } : {}) });
      if (result.requires_model_selection) {
        const candidates = result.candidates?.length ? result.candidates : state.models;
        if (!candidates.length) throw new Error("没有可用模型，请先在设置中添加模型。");
        return await openModal("选择目标模型", `<label class="field">目标模型<select id="parameterTargetModel"><option value="">请选择模型</option>${candidates.map((model) => `<option value="${escape(model.model_ref)}">${escape(model.name || model.id)} · ${escape(model.provider_name || model.provider_id || "")}</option>`).join("")}</select></label>${(result.warnings || []).length ? `<p class="import-warnings">${result.warnings.map(escape).join("<br>")}</p>` : ""}`, [{ label: "取消", action: () => false }, { label: "填入参数", primary: true, action: async () => {
          const target = $("parameterTargetModel").value; if (!target) throw new Error("请选择目标模型。");
          const resolved = await apiPost("studio/parameters/resolve", { content, model_ref: target });
          if (resolved.requires_model_selection) throw new Error("此模型无法接收当前参数，请选择其他模型。");
          applyResolved(resolved); return true;
        } }], { focus: "parameterTargetModel" });
      }
      applyResolved(result); return true;
    }

    function applyResolved(result) {
      if (!result.draft) throw new Error("参数解析结果缺少可填写的内容。");
      hooks.applyDraft(result.draft);
      const warnings = result.warnings || [];
      const unmapped = result.unmapped || {};
      const notice = $("parameterImportNotice");
      notice.innerHTML = `${warnings.map((warning) => `<p>${escape(warning)}</p>`).join("")}${Object.keys(unmapped).length ? `<details open><summary>未映射参数</summary><pre>${escape(serial(unmapped))}</pre></details>` : ""}`;
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

    function renderGalleryCard(item) {
      const warning = !!item.cleanup_warning;
      const selected = state.selectedIds.has(item.id);
      return `<article class="gallery-card ${item.is_favorite ? "is-favorite" : ""} ${warning ? "has-cleanup-warning" : ""} ${selected ? "is-selected" : ""}" data-gallery-id="${escape(item.id)}" tabindex="0" role="button" aria-label="查看 ${escape(item.model || item.provider_name || "图片")}">
        <div class="gallery-image-wrap">${item.thumbnail_data_url ? `<img src="${escape(item.thumbnail_data_url)}" alt="${escape(item.prompt_preview)}" loading="lazy" />` : `<div class="gallery-missing-image">${icon("Image")}<span>图片不可用</span></div>`}
          <label class="gallery-selection" title="选择生成记录"><input type="checkbox" data-select-id="${escape(item.id)}" aria-label="选择生成记录" ${selected ? "checked" : ""} /><span>${icon("Check")}</span></label>
          <span class="gallery-source-label">${escape(engineLabel(item.generation_engine))}</span>${Number(item.image_count) > 1 ? `<span class="gallery-image-count" title="${Number(item.image_count)} 张图片">${icon("Image")}<span>${Number(item.image_count)}</span></span>` : ""}${item.is_favorite ? `<span class="gallery-favorite" title="已收藏" aria-label="已收藏">${icon("Star")}</span>` : ""}
        </div><div class="gallery-info"><strong>${escape(item.model || item.provider_name || engineLabel(item.generation_engine))}</strong><p>${escape(item.prompt_preview || "无提示词")}</p><div class="gallery-meta"><span>${modeLabel(item.mode)}</span><span>${formatDate(item.created_at)}</span></div>${warning ? '<span class="cleanup-warning-label">清理候选</span>' : ""}${item.file_state && item.file_state !== "available" ? '<span class="cleanup-warning-label">文件需检查</span>' : ""}</div></article>`;
    }

    function galleryRendered(payload) {
      const notice = $("galleryRetention");
      const retention = payload.retention || {};
      notice.textContent = retention.near_limit ? (retention.message || "历史容量接近保留上限，清理候选记录可能被自动删除。") : "";
      notice.classList.toggle("is-hidden", !retention.near_limit);
      $("galleryGrid").querySelectorAll("[data-gallery-id]").forEach((card) => card.addEventListener("keydown", (event) => { if (event.target !== card) return; if (["Enter", " "].includes(event.key)) { event.preventDefault(); detailTrigger = card; void hooks.openDetail(card.dataset.galleryId); } }));
      syncFloatingBars();
    }

    function syncFloatingBars() {
      const galleryBar = document.querySelector(".gallery-floatingbar");
      galleryBar.classList.toggle("is-hidden", $("galleryPagination").classList.contains("is-hidden"));
      $("galleryView").classList.toggle("has-selection", state.selectedIds.size > 0);
      $("galleryGrid").querySelectorAll("[data-gallery-id]").forEach((card) => card.classList.toggle("is-selected", state.selectedIds.has(card.dataset.galleryId)));
      syncSelectionHeader();
    }

    function syncSelectionHeader() {
      const title = document.querySelector(".topbar");
      const active = state.view === "gallery" && !$("selectionBar").classList.contains("is-hidden");
      let shift = 0;
      let progress = 0;
      if (active) {
        // The anchor stays in normal flow after the action bar becomes sticky.
        const top = parseFloat(getComputedStyle(title).top) || 0;
        const distance = title.offsetHeight + 10;
        shift = Math.max(0, Math.min(distance, top + distance - $("selectionAnchor").getBoundingClientRect().top));
        progress = shift / distance;
      }
      title.style.setProperty("--selection-title-offset", `${-shift}px`);
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
      renderIcons();
      window.addEventListener("scroll", scheduleSelectionHeader, { passive: true });
      window.addEventListener("resize", scheduleSelectionHeader, { passive: true });
      if (window.ResizeObserver) {
        const observer = new ResizeObserver(scheduleSelectionHeader);
        for (const element of [document.querySelector(".topbar"), document.querySelector(".gallery-toolbar"), $("selectionBar")]) observer.observe(element);
      }
      for (const [view, name] of Object.entries({ generate: "Sparkles", gallery: "Image", import: "FolderInput", settings: "Settings2" })) {
        const item = document.querySelector(`.nav-item[data-view="${view}"] .nav-icon`); item.className = "nav-icon"; item.innerHTML = icon(name);
      }
      for (const [id, name, label] of [["galleryRefresh", "RefreshCw", "刷新画廊"], ["galleryPrev", "ChevronLeft", "上一页"], ["galleryNext", "ChevronRight", "下一页"], ["exportButton", "Download", "导出"], ["selectAllButton", "CheckCheck", "全选当前页"], ["cancelSelectionButton", "X", "取消选择"], ["deleteButton", "Trash2", "删除所选记录"], ["saveSettingsButton", "Check", "保存全部设置"], ["confirmImportButton", "Upload", "确认导入"], ["cancelImportButton", "X", "取消导入"]]) {
        const button = $(id); button.innerHTML = `${icon(name)}<span>${label}</span>`; button.setAttribute("aria-label", label); button.title = label; button.classList.add("responsive-command");
      }
      const formatWrapper = document.createElement("label"); formatWrapper.className = "copy-format-picker"; formatWrapper.title = "选择参数格式"; formatWrapper.innerHTML = icon("FileJson");
      $("detailCopyFormat").before(formatWrapper); formatWrapper.appendChild($("detailCopyFormat"));
      $("chooseImportFiles").addEventListener("click", () => $("importFiles").click());
      $("importFiles").addEventListener("change", (event) => { void addImportFiles(event.target.files); event.target.value = ""; });
      $("importDropzone").addEventListener("click", () => $("importFiles").click());
      $("confirmImportButton").addEventListener("click", () => void confirmImports());
      $("cancelImportButton").addEventListener("click", clearImports);
      $("importAsGroup").addEventListener("change", () => { void discardImportGroup(); });
      $("importGrid").addEventListener("click", (event) => { const button = event.target.closest("[data-remove-import]"); if (button) removeImport(button.dataset.removeImport); });
      $("importGrid").addEventListener("input", (event) => {
        const key = event.target.dataset.importField; if (!key) return;
        void discardImportGroup();
        const item = imports.find((entry) => entry.id === event.target.closest("[data-import-id]").dataset.importId); if (item) item.fields[key] = event.target.value;
      });
      for (const view of [$("galleryView"), $("importView")]) {
        view.addEventListener("dragover", (event) => { if (!Array.from(event.dataTransfer.types).includes("Files")) return; event.preventDefault(); view.classList.add("is-drop-target"); });
        view.addEventListener("dragleave", (event) => { if (!view.contains(event.relatedTarget)) view.classList.remove("is-drop-target"); });
        view.addEventListener("drop", (event) => { event.preventDefault(); view.classList.remove("is-drop-target"); void addImportFiles(event.dataTransfer.files); });
      }
      $("galleryEngine").addEventListener("change", () => { hooks.clearGallerySelection(); void hooks.loadGallery(0); });
      $("galleryFavorite").addEventListener("change", () => { hooks.clearGallerySelection(); void hooks.loadGallery(0); });
      $("pasteParametersButton").addEventListener("click", () => void readClipboardParameters());
      $("detailFavorite").addEventListener("click", () => void toggleFavorite());
      $("detailCopy").addEventListener("click", () => void copyDetailFormat());
      $("detailWorkflowDownload").addEventListener("click", () => void copyDetailFormat(true));
      $("detailReproduce").addEventListener("click", () => void hooks.reproduce(state.detailId));
      $("detailUseReference").addEventListener("click", () => { const image = state.detailData?.images?.[state.detailImageIndex]; if (image?.data_url) void hooks.useDataUrlAsReference(image.data_url, "gallery-output-reference.png"); });
      $("detailDelete").addEventListener("click", () => void deleteDetailImages());
      $("drawerBody").addEventListener("click", (event) => { const button = event.target.closest("[data-copy-field]"); if (button) void copyText(detailCopies[Number(button.dataset.copyField)]); });
      $("studioModalClose").addEventListener("click", () => modalClose?.(false));
      $("studioModalRoot").querySelector(".studio-modal-scrim").addEventListener("click", () => modalClose?.(false));
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
      document.addEventListener("keydown", (event) => {
        if (document.querySelector(".pswp--open")) return;
        if (modalClose && event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); modalClose(false); return; }
        if (event.key !== "Tab") return;
        const modal = modalClose ? $("studioModal") : !$("confirmDialog").classList.contains("is-hidden") ? $("confirmDialog") : !$("parameterDialog").classList.contains("is-hidden") ? $("parameterDialog") : !$("imagePreview").classList.contains("is-hidden") ? $("imagePreview") : $("detailDrawer").classList.contains("is-open") ? $("detailDrawer") : null;
        if (!modal) return;
        const focusable = Array.from(modal.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')).filter((item) => item.getClientRects().length);
        if (!focusable.length) { event.preventDefault(); modal.focus(); return; }
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
      }, true);
      window.addEventListener("beforeunload", () => imports.forEach((item) => URL.revokeObjectURL(item.url)));
    }

    return { bind, modeLabel, engineLabel, galleryPageSize, renderGalleryCard, galleryRendered, syncFloatingBars, detailMetadataMarkup, updateDetailActions, copyText, resolveParameters, setCommandLabel, modalOpen: () => !!modalClose };
  };
  window.ImageStudioMetadata = { extractMetadata, decodeComment };
})();
