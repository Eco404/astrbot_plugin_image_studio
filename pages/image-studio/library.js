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

  function decodeComment(value) {
    if (typeof value === "string") return value.replace(/\0+$/, "");
    if (!Array.isArray(value) && !ArrayBuffer.isView(value)) return "";
    if (Array.isArray(value) && value.every((item) => typeof item === "string")) return value.join("").replace(/\0+$/, "");
    const bytes = value instanceof Uint8Array ? value : Uint8Array.from(value);
    const signature = String.fromCharCode(...bytes.slice(0, 8));
    const payload = /^(UNICODE\0|ASCII\0\0\0|JIS\0\0\0\0\0)/.test(signature) ? bytes.slice(8) : bytes;
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
    for (const name of ["Software", "ImageDescription", "Make", "Model", "Artist", "Copyright", "UserComment", "DateTime", "DateTimeOriginal", "DateTimeDigitized", "OffsetTime", "OffsetTimeOriginal", "OffsetTimeDigitized", "SubSecTime", "SubSecTimeOriginal", "SubSecTimeDigitized"]) {
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
    const { state, escape, apiGet, apiPost, bridge, showNotice, errorMessage, formatDate, formatBytes, getGallerySort } = hooks;
    let imports = [];
    let importing = false;
    let importSequence = 0;
    let importGroupDraft = null;
    let importAddQueue = Promise.resolve();
    let importAddJobs = 0;
    let importEpoch = 0;
    let importBatchJob = null;
    let importBatchResult = null;
    let modalClose = null;
    let modalDismissOutside = true;
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
    let importEditor = null;
    let importEditLoading = false;
    // Serialized server snapshots are immutable and separate from editable drafts.
    // Each reopen validates item revisions with a fresh lightweight manifest.
    const editSnapshots = new Map();
    const editSnapshotReads = new Map();
    let editSnapshotBytes = 0;
    const EDIT_SNAPSHOT_LIMIT = 16 * 1024 * 1024;
    const EDIT_SNAPSHOT_COUNT = 64;

    function editMediaItem(item) { return { id: item.imageId, sha256: item.sha256, thumbnail_revision: item.thumbnailRevision }; }
    function editSnapshotKey(context, item) { return `${context.generationId}:${item.imageId}:${item.itemRevision}`; }

    function removeEditSnapshot(key) {
      const value = editSnapshots.get(key);
      if (value) { editSnapshotBytes -= value.bytes; editSnapshots.delete(key); }
    }

    function rememberEditSnapshot(context, item, entry) {
      const snapshot = { ...entry };
      if (hooks.cacheImageMedia && snapshot.thumbnail_data_url) {
        hooks.cacheImageMedia(editMediaItem(item), "preview", snapshot.thumbnail_data_url);
        delete snapshot.thumbnail_data_url;
      }
      const text = JSON.stringify(snapshot), bytes = text.length * 2;
      const key = editSnapshotKey(context, item);
      removeEditSnapshot(key);
      if (bytes > EDIT_SNAPSHOT_LIMIT) return snapshot;
      while (editSnapshots.size && (editSnapshotBytes + bytes > EDIT_SNAPSHOT_LIMIT || editSnapshots.size >= EDIT_SNAPSHOT_COUNT)) removeEditSnapshot(editSnapshots.keys().next().value);
      editSnapshots.set(key, { text, bytes }); editSnapshotBytes += bytes;
      return JSON.parse(text);
    }

    async function readEditSnapshot(context, item) {
      const key = editSnapshotKey(context, item);
      const cached = editSnapshots.get(key);
      if (cached) {
        editSnapshots.delete(key); editSnapshots.set(key, cached);
        return JSON.parse(cached.text);
      }
      let pending = editSnapshotReads.get(key);
      if (!pending) {
        pending = (async () => {
          const preview = hooks.getImageMedia?.(editMediaItem(item), "preview");
          const record = await apiGet(`gallery/import-edit/${context.generationId}`, { image_id: item.imageId, item_revision: item.itemRevision, ...(preview ? { include_preview: 0 } : {}) });
          const entry = record.items?.find((entry) => entry.image_id === item.imageId);
          if (!entry?.fields || entry.item_revision !== item.itemRevision) throw new Error("图片参数响应不完整或版本已变化，请重新打开编辑器。");
          return rememberEditSnapshot(context, item, entry);
        })();
        editSnapshotReads.set(key, pending);
        void pending.finally(() => { if (editSnapshotReads.get(key) === pending) editSnapshotReads.delete(key); }).catch(() => {});
      }
      // Concurrent/reopened editors must never share mutable nested metadata.
      return JSON.parse(JSON.stringify(await pending));
    }
    // Uploads and persisted edits share controls, but never share draft state.
    const importContext = {
      get items() { return imports; }, set items(value) { imports = value; },
      get saving() { return importing; }, get addJobs() { return importAddJobs; },
      get epoch() { return importEpoch; },
      get batchJob() { return importBatchJob; }, set batchJob(value) { importBatchJob = value; },
      get batchResult() { return importBatchResult; }, set batchResult(value) { importBatchResult = value; },
      grid: () => $("importGrid"), active: () => true, render: renderImports,
      invalidate: discardImportGroup, sorter: null, editing: false,
    };
    function cardsBusy(context) { return context.saving || !!context.batchJob; }
    function sortEnabled(context) { return context.active() && !cardsBusy(context) && !context.addJobs && context.items.length > 1 && !context.items.some((item) => item.status === "reading"); }

    function modeLabel(mode) { return ({ text2img: "文生图", img2img: "图生图" })[mode] || "未知模式"; }
    function engineLabel(engine) { return engine === "nai" ? ENGINES.novelai : engine === "mixed" ? "混合来源" : ENGINES[engine] || engine || "未知来源"; }
    function engineOf(detail) { const engine = detail.generation_engine || (detail.provider_kind === "nai_direct" ? "novelai" : "unknown"); return engine === "nai" ? "novelai" : engine; }
    function options(values, selected) { return Object.entries(values).map(([value, label]) => `<option value="${escape(value)}" ${String(selected ?? "") === value ? "selected" : ""}>${escape(label)}</option>`).join(""); }
    function setCommandLabel(id, label) { const button = $(id); const span = button.querySelector("span"); if (span) span.textContent = label; else button.textContent = label; button.setAttribute("aria-label", label); button.dataset.tooltip = label; button.dataset.tooltipOverflow = span ? ":scope > span:last-child" : ""; }

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
      if (modalClose) modalClose(false, true);
      const previousFocus = document.activeElement;
      const root = $("studioModalRoot"), modal = $("studioModal");
      $("studioModalTitle").textContent = title;
      $("studioModalBody").innerHTML = body;
      $("studioModalError").textContent = "";
      $("studioModalFooter").innerHTML = "";
      modal.removeAttribute("aria-busy");
      $("studioModalBody").scrollTop = 0;
      $("studioModal").classList.toggle("is-merge-picker", !!options.mergePicker);
      $("studioModal").classList.toggle("is-import-editor", !!options.importEditor);
      $("studioModal").classList.toggle("is-external-editor", !!options.externalEditor);
      modalDismissOutside = options.dismissOutside !== false;
      window.ImageStudioDialogMotion.show(root);
      hooks.syncPageScrollLock();
      return new Promise((resolve) => {
        let pending = false, closing = false;
        const close = (result, immediate = false) => {
          if (modalClose !== close || (!immediate && (pending || closing))) return;
          window.ImageStudioSelect?.close();
          if (!closing) options.onClose?.();
          closing = true;
          const finish = () => {
            if (modalClose !== close) return;
            modalClose = null;
            if (!immediate) {
              hooks.syncPageScrollLock();
              if (previousFocus?.isConnected) previousFocus.focus?.({ preventScroll: true });
            }
            resolve(result);
          };
          if (immediate) { window.ImageStudioDialogMotion.hideImmediately(root); finish(); }
          else window.ImageStudioDialogMotion.hide(root, finish);
        };
        modalClose = close;
        for (const definition of actions) {
          const button = document.createElement("button");
          button.type = "button"; button.className = definition.danger ? "danger-button" : definition.primary ? "primary-button" : "quiet-button";
          button.textContent = definition.label;
          if (definition.id) button.id = definition.id;
          button.disabled = typeof definition.disabled === "function" ? definition.disabled() : !!definition.disabled;
          button.addEventListener("click", async () => {
            if (modalClose !== close || pending || closing) return;
            pending = true; modal.setAttribute("aria-busy", "true");
            $("studioModalFooter").querySelectorAll("button").forEach((item) => { item.disabled = true; });
            try { const result = await definition.action(); pending = false; if (result !== undefined) close(result); }
            catch (error) { if (modalClose === close) $("studioModalError").textContent = errorMessage(error, "操作失败"); }
            finally {
              pending = false;
              if (modalClose === close) { modal.removeAttribute("aria-busy"); $("studioModalFooter").querySelectorAll("button").forEach((item, index) => { const disabled = actions[index]?.disabled; item.disabled = typeof disabled === "function" ? disabled() : !!disabled; }); }
            }
          });
          $("studioModalFooter").appendChild(button);
        }
        renderIcons($("studioModal"));
        options.onOpen?.();
        window.ImageStudioSelect?.refresh($("studioModal"));
        (options.focus ? $(options.focus) : $("studioModalFooter").querySelector("button"))?.focus({ preventScroll: true });
      });
    }

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

    function importCard(item, index, context = importContext) {
      const data = item.fields;
      const warnings = [...(item.parsed?.warnings || []), ...(item.warning ? [item.warning] : [])];
      const disabled = cardsBusy(context) || item.status === "reading";
      const saveOutputs = (item.parsed?.normalized?.outputs || []).filter((entry) => entry.kind === "save");
      const selectedOutput = saveOutputs.find((entry) => String(entry.node_id) === String(item.outputNodeId || ""));
      const outputChoice = saveOutputs.length > 1 ? `<div class="field field-wide"><label for="${item.id}-output">最终保存输出</label><div class="import-output-choice"><select id="${item.id}-output" data-import-output aria-label="最终保存输出"><option value="">请选择保存输出</option>${saveOutputs.map((entry) => `<option value="${escape(entry.node_id)}" ${String(entry.node_id) === String(item.outputNodeId || "") ? "selected" : ""}>${escape(entry.type)} #${escape(entry.node_id)}</option>`).join("")}</select><button class="quiet-button import-batch-button" data-batch-import-output type="button" aria-label="批量应用保存输出到全部匹配图片" data-tooltip="将保存输出选择应用到全部匹配图片（含当前图片）" ${batchChoiceDisabled(item, context) || !selectedOutput?.match_key ? "disabled" : ""}>${icon("CheckCheck")}</button></div></div>` : "";
      return `<article class="import-card glass ${item.duplicateReason ? "is-duplicate" : ""}" data-import-id="${item.id}" data-import-sha256="${item.sha256}">
        <div class="import-card-header"><button class="studio-icon-button import-sort-handle" data-sort-handle type="button" aria-label="调整第 ${index + 1} 张图片顺序：${escape(item.file.name)}" data-tooltip="拖动排序，也可聚焦后使用方向键" ${sortEnabled(context) ? "" : "disabled"}>${icon("GripVertical")}</button><span class="import-order" aria-label="第 ${index + 1} 张">${index + 1}</span><strong data-tooltip="${escape(item.file.name)}" data-tooltip-overflow>${escape(item.file.name)}</strong>${context.editing ? "" : `<button class="studio-icon-button is-danger" data-remove-import="${item.id}" type="button" aria-label="移除 ${escape(item.file.name)}" data-tooltip="移除图片" ${importing ? "disabled" : ""}>${icon("X")}</button>`}</div>
        <div class="import-card-preview" data-sort-surface><img src="${escape(item.url)}" draggable="false" alt="${escape(item.file.name)}" /></div><div class="import-file-meta">${formatBytes(item.file.size)}${item.width ? ` · ${item.width} × ${item.height}` : ""}</div>
        <fieldset class="import-card-fields" ${disabled ? "disabled" : ""}>
          ${outputChoice}
          <label class="field">生图来源<select data-import-field="generation_engine">${options(ENGINES, data.generation_engine)}</select></label>
          <label class="field">模型<input data-import-field="model" value="${escape(data.model)}" /></label>
          <label class="field">模式<select data-import-field="mode">${options({ unknown: "未知模式", text2img: "文生图", img2img: "图生图" }, data.mode || "unknown")}</select></label>
          <label class="field">生成时间<input data-import-field="generated_at" type="datetime-local" step="0.001" value="${escape(data.generated_at || "")}" /></label>
          <label class="field field-wide">正向提示词${item.editedFields.has("prompt") ? "" : promptStatusMarkup(item.parsed?.normalized?.prompt_status)}<textarea data-import-field="prompt" rows="3">${escape(data.prompt)}</textarea></label>
          <label class="field field-wide">反向提示词${item.editedFields.has("negative_prompt") ? "" : promptStatusMarkup(item.parsed?.normalized?.negative_prompt_status)}<textarea data-import-field="negative_prompt" rows="2">${escape(data.negative_prompt)}</textarea></label>
          <details class="field-wide advanced" ${context.editing ? 'data-import-deferred="parameters"' : ""}><summary>补充参数</summary>${context.editing ? "" : `<textarea data-import-field="parameters" rows="5" spellcheck="false" aria-label="补充参数 JSON">${escape(data.parameters)}</textarea>`}</details>
        </fieldset>${promptCandidatesMarkup(item, context)}${context.editing && item.parsed?.normalized?.stages?.length ? `<details class="comfy-workflow-info" data-import-deferred="workflow"><summary>采样阶段与条件 · ${item.parsed.normalized.stages.length} 个阶段</summary></details>` : comfyDetailsMarkup(item.parsed)}<div class="import-card-status ${item.status === "error" || item.duplicateReason ? "is-error" : ""}" role="status">${escape(item.duplicateReason || (item.status === "reading" ? "正在识别图片参数…" : item.error || (item.parsed?.format && item.parsed.format !== "unknown" ? `已识别 ${engineLabel(item.parsed.format)}` : "未检测到生图参数，可手动填写")))}</div>${warnings.length ? `<div class="import-warnings">${warnings.map(escape).join("<br>")}</div>` : ""}</article>`;
    }

    function hasPromptBlock(value, block) {
      const normalize = (text) => String(text || "").replace(/\r\n/g, "\n").trim();
      const text = normalize(block);
      return !!text && (`\n\n${normalize(value)}\n\n`).includes(`\n\n${text}\n\n`);
    }

    function snapshotSelection(item, candidate, target) {
      return candidate.status === "display_snapshot" && candidate.source_ref ? item.snapshotSelections?.[target]?.[candidate.source_ref] : null;
    }

    function candidateAction(item, candidate, target, context = importContext) {
      const exists = hasPromptBlock(item.fields[target], candidate.text);
      return { disabled: exists || cardsBusy(context) || item.status === "reading", label: `${exists ? "已填入" : snapshotSelection(item, candidate, target) ? "改用" : "添加到"}${target === "prompt" ? "正向" : "反向"}` };
    }

    function batchChoiceDisabled(item, context = importContext) { return cardsBusy(context) || item.status === "reading" || !context.items.includes(item); }

    function snapshotMarkup(candidate) {
      if (candidate.status !== "display_snapshot") return "";
      const sources = [...new Set((candidate.observations || []).map((observation) => `${({ prompt: "API 备用显示值", workflow: "工作流执行回写" })[observation.source] || "图片元数据"} · ${observation.node_type} #${observation.node_id}`))];
      const kind = candidate.snapshot_kind === "api_fallback" ? "API 备用显示值，可能来自上一次运行。" : candidate.snapshot_kind === "workflow" ? "工作流执行回写候选。" : "";
      return `<div class="prompt-candidate-meta">关联输出 ${escape(candidate.source_ref)}${sources.length ? `<br>${sources.map(escape).join("<br>")}` : ""}</div><p class="prompt-candidate-snapshot-note">${kind}未验证是否为本次结果${candidate.conflicting ? "。同一输出存在不同快照，请核对并选择其中一份。" : "，请核对后再填入。"}</p>`;
    }

    // Keep track of the exact block inserted by a snapshot, including subsequent
    // user edits. This lets another snapshot replace that block without touching
    // unrelated notes or mistaking edited text for the original cached value.
    function updatePromptField(item, target, value) {
      const before = item.fields[target] || "";
      const selections = item.snapshotSelections?.[target] || {};
      let start = 0, suffix = 0;
      while (start < before.length && start < value.length && before[start] === value[start]) start++;
      while (suffix < before.length - start && suffix < value.length - start && before[before.length - suffix - 1] === value[value.length - suffix - 1]) suffix++;
      const oldEnd = before.length - suffix, newEnd = value.length - suffix, delta = value.length - before.length;
      for (const [source, selection] of Object.entries(selections)) {
        if (oldEnd <= selection.start) { selection.start += delta; selection.end += delta; }
        else if (start < selection.end) {
          selection.start = Math.min(selection.start, start);
          selection.end = oldEnd >= selection.end ? newEnd : selection.end + delta;
        }
        if (!value || selection.end <= selection.start) delete selections[source];
      }
      item.fields[target] = value;
    }

    function snapshotBlockIntact(value, selection) {
      const before = value.slice(0, selection.start), after = value.slice(selection.end);
      return value.slice(selection.start, selection.end) === selection.text
        && (!before.trim() || /\n[ \t]*\n[ \t]*$/.test(before))
        && (!after.trim() || /^[ \t]*\n[ \t]*\n/.test(after));
    }

    function promptCandidatesMarkup(item, context = importContext, expanded = false) {
      if (item.parsed?.format !== "comfyui") return "";
      const normalized = item.parsed.normalized || {};
      const candidates = normalized.prompt_candidates || [];
      if (!candidates.length && !normalized.requires_output_selection) return "";
      if (context.editing && !expanded) return `<details class="import-prompt-candidates" data-import-deferred="candidates"><summary>候选提示词 · ${candidates.length} 项</summary></details>`;
      const entries = candidates.map((candidate) => {
        const role = { positive: "正向链路", negative: "反向链路", mixed: "正反向链路", unknown: "方向未确定" }[candidate.role] || "方向未确定";
        const status = { static: "静态文本", template: "动态模板", unknown_path: "途经未知节点", display_snapshot: "关联显示快照" }[candidate.status] || "作用未确定";
        const covered = (candidate.covered_candidates || []).map((entry) => `${entry.node_type} #${entry.node_id} · ${entry.field}`);
        const actions = ["prompt", "negative_prompt"].map((target) => {
          const action = candidateAction(item, candidate, target, context);
          const direction = target === "prompt" ? "正向" : "反向";
          return `<div class="prompt-candidate-split"><button class="quiet-button" data-candidate-id="${escape(candidate.id)}" data-candidate-target="${target}" type="button" ${action.disabled ? "disabled" : ""}>${action.label}</button><button class="quiet-button import-batch-button" data-batch-candidate-id="${escape(candidate.id)}" data-batch-candidate-target="${target}" type="button" aria-label="批量应用${direction}候选到全部匹配图片" data-tooltip="将此节点选择应用到全部匹配图片的${direction}提示词（含当前图片），使用各图片自己的文本" ${batchChoiceDisabled(item, context) || !candidate.match_key ? "disabled" : ""}>${icon("CheckCheck")}</button></div>`;
        }).join("");
        return `<div class="prompt-candidate" data-prompt-candidate="${escape(candidate.id)}"><div class="prompt-candidate-title"><strong>${escape(candidate.node_type)} #${escape(candidate.node_id)}</strong><span>${escape(candidate.field)}</span></div><div class="prompt-candidate-meta">${role} · ${status}${candidate.stage_ids?.length ? ` · 阶段 ${candidate.stage_ids.map(escape).join("、")}` : ""}</div>${snapshotMarkup(candidate)}${covered.length ? `<div class="prompt-candidate-meta">已包含上游 ${covered.map(escape).join("、")}</div>` : ""}<details class="prompt-candidate-text"><summary>${escape(candidate.text)}</summary><pre>${escape(candidate.text)}</pre></details><div class="prompt-candidate-actions">${actions}</div></div>`;
      }).join("");
      return `<details class="import-prompt-candidates"><summary>候选提示词 · ${candidates.length} 项</summary>${normalized.requires_output_selection ? '<p class="field-hint">尚未选择最终保存输出</p>' : entries}</details>`;
    }

    function syncCandidateButtons(card, item, context = importContext) {
      for (const target of ["prompt", "negative_prompt"]) if (item.editedFields.has(target)) card.querySelector(`[data-import-field="${target}"]`)?.parentElement.querySelector(".comfy-summary-status")?.remove();
      card.querySelectorAll("[data-candidate-target]").forEach((button) => {
        const candidate = item.parsed?.normalized?.prompt_candidates?.find((entry) => entry.id === button.dataset.candidateId);
        if (!candidate) return;
        const target = button.dataset.candidateTarget;
        const action = candidateAction(item, candidate, target, context);
        button.disabled = action.disabled;
        button.textContent = action.label;
      });
    }

    function applyPromptCandidate(item, candidate, target) {
      if (!candidate?.text) return { status: "mismatch", message: "对应节点没有可用文本" };
      if (hasPromptBlock(item.fields[target], candidate.text)) return { status: "already", message: "该图片的对应文本已填入" };
      const current = item.fields[target] || "";
      const previous = snapshotSelection(item, candidate, target);
      if (previous && !snapshotBlockIntact(current, previous)) {
        return { status: "protected", message: "之前填入的显示快照已被手动修改。请先核对并删除该片段，或清空该方向的提示词，再选择其他快照。" };
      }
      const text = String(candidate.text).replace(/\r\n/g, "\n");
      const start = previous ? previous.start : current ? current.length + 2 : 0;
      const value = previous ? `${current.slice(0, previous.start)}${text}${current.slice(previous.end)}` : current ? `${current}\n\n${text}` : text;
      updatePromptField(item, target, value);
      if (candidate.status === "display_snapshot" && candidate.source_ref) {
        item.snapshotSelections ||= {};
        item.snapshotSelections[target] ||= {};
        item.snapshotSelections[target][candidate.source_ref] = { id: candidate.id, text, start, end: start + text.length };
      }
      item.editedFields.add(target);
      item.revision = (item.revision || 0) + 1;
      return { status: "applied", message: `已${previous ? "替换" : "添加"}该图片的对应文本`, replaced: !!previous };
    }

    function addPromptCandidate(button, context = importContext) {
      const card = button.closest("[data-import-id]");
      const item = context.items.find((entry) => entry.id === card?.dataset.importId);
      const target = button.dataset.candidateTarget;
      if (!item || cardsBusy(context) || item.status === "reading" || !["prompt", "negative_prompt"].includes(target)) return;
      const candidate = item.parsed?.normalized?.prompt_candidates?.find((entry) => entry.id === button.dataset.candidateId);
      const result = applyPromptCandidate(item, candidate, target);
      if (result.status === "already") return;
      if (result.status !== "applied") { showNotice(result.message, "error"); return; }
      void context.invalidate();
      card.querySelector(`[data-import-field="${target}"]`).value = item.fields[target];
      syncCandidateButtons(card, item, context);
      showNotice(`候选已${result.replaced ? "替换到" : "添加到"}${target === "prompt" ? "正向" : "反向"}提示词。`, "success");
    }

    function observationKey(observation) { return JSON.stringify([observation.source, observation.node_id, observation.node_type, observation.field]); }

    function matchingPromptCandidate(item, source) {
      if (!source.match_key) return { status: "mismatch", message: "来源节点没有可匹配的链路信息" };
      if (item.parsed?.normalized?.requires_output_selection) return { status: "mismatch", message: "尚未选择保存分支，请先为该图片选择最终保存输出" };
      let candidates = (item.parsed?.normalized?.prompt_candidates || []).filter((candidate) => candidate.match_key === source.match_key);
      if (source.status === "display_snapshot") {
        const origins = new Set((source.observations || []).map(observationKey));
        candidates = candidates.filter((candidate) => (candidate.observations || []).some((observation) => origins.has(observationKey(observation))));
      }
      if (!candidates.length) return { status: "mismatch", message: "当前保存分支没有对应节点、端口或快照来源" };
      if (candidates.length > 1) return { status: "conflict", message: "对应来源在该图片中存在多份冲突快照，请单独选择" };
      return { candidate: candidates[0] };
    }

    function startImportBatch(source, title, context = importContext) {
      if (!source || batchChoiceDisabled(source, context)) return null;
      const job = { title, context, epoch: context.epoch, entries: context.items.map((item) => ({ item, revision: item.revision || 0, parsed: item.parsed, status: item.status, hydrated: !context.editing || item.hydrated })), results: [], loaded: 0 };
      context.batchJob = job;
      context.batchResult = null;
      context.render();
      return job;
    }

    function batchEntryChanged(job, entry) {
      if (job.epoch !== job.context.epoch || !job.context.active() || !job.context.items.includes(entry.item)) return "图片已移除或本次导入已取消";
      if ((entry.item.revision || 0) !== entry.revision || entry.item.parsed !== entry.parsed) return "图片在操作期间已编辑或重新解析，保留当前内容";
      if (entry.status === "reading") return "点击批量按钮时仍在读取图片，请读取完成后重试";
      return "";
    }

    async function hydrateBatchEntry(job, entry) {
      if (!job.context.editing || entry.hydrated) return batchEntryChanged(job, entry);
      if (job.epoch !== job.context.epoch || !job.context.active() || !job.context.items.includes(entry.item)) return "图片已移除或编辑已取消";
      if ((entry.item.revision || 0) !== entry.revision) return "图片在操作期间已编辑，保留当前内容";
      const loaded = await ensureImportEditorItem(job.context, entry.item);
      if (!loaded) return entry.item.error || "图片读取失败，请重试";
      if ((entry.item.revision || 0) !== entry.revision) return "图片在操作期间已编辑，保留当前内容";
      entry.parsed = entry.item.parsed; entry.status = entry.item.status; entry.hydrated = true;
      return batchEntryChanged(job, entry);
    }

    function batchResultSummary(result) {
      const labels = { applied: "已应用", already: "已填入或已选择", mismatch: "节点不匹配", conflict: "快照冲突", protected: "手动修改保护", failed: "读取失败", skipped: "已跳过" };
      const parts = Object.entries(labels).map(([status, label]) => { const count = result.results.filter((entry) => entry.status === status).length; return count ? `${label} ${count} 张` : ""; }).filter(Boolean);
      const manual = result.results.filter((entry) => entry.manualReview).length;
      if (manual) parts.push(`其中 ${manual} 张保留手动内容，请核对`);
      return parts.join("，");
    }

    function finishImportBatch(job, context = importContext) {
      if (context.batchJob === job) context.batchJob = null;
      if (job.epoch === context.epoch && context.active()) {
        context.batchResult = job;
        showNotice(batchResultSummary(job), job.results.some((entry) => entry.status === "applied") ? "success" : "info");
      }
      context.render();
    }

    async function applyCandidateToImports(button, context = importContext) {
      const source = context.items.find((item) => item.id === button.closest("[data-import-id]")?.dataset.importId);
      const target = button.dataset.batchCandidateTarget;
      const candidate = source?.parsed?.normalized?.prompt_candidates?.find((entry) => entry.id === button.dataset.batchCandidateId);
      if (!candidate?.match_key || !["prompt", "negative_prompt"].includes(target)) return;
      const job = startImportBatch(source, `批量应用${target === "prompt" ? "正向" : "反向"}候选`, context);
      if (!job) return;
      try {
        await context.invalidate();
        const processEntry = async (entry) => {
          const changed = await hydrateBatchEntry(job, entry);
          const matched = changed ? { status: "skipped", message: changed } : entry.item === source ? { candidate } : matchingPromptCandidate(entry.item, candidate);
          const result = matched.candidate ? applyPromptCandidate(entry.item, matched.candidate, target) : matched;
          job.results.push({ filename: entry.item.file.name, ...result });
          job.loaded++; context.render();
        };
        for (let index = 0; index < job.entries.length; index += 3) await Promise.all(job.entries.slice(index, index + 3).map(processEntry));
      } finally { finishImportBatch(job, context); }
    }

    async function applyOutputToImports(button, context = importContext) {
      const source = context.items.find((item) => item.id === button.closest("[data-import-id]")?.dataset.importId);
      const output = source?.parsed?.normalized?.outputs?.find((entry) => entry.kind === "save" && String(entry.node_id) === String(source.outputNodeId || ""));
      if (!output?.match_key) return;
      const job = startImportBatch(source, "批量应用保存输出", context);
      if (!job) return;
      try {
        await context.invalidate();
        const processEntry = async (entry) => {
          const item = entry.item;
          const record = (result) => { job.results.push({ filename: item.file.name, ...result }); };
          let changed = await hydrateBatchEntry(job, entry);
          if (changed) { record({ status: "skipped", message: changed }); return; }
          const matching = (item.parsed?.normalized?.outputs || []).filter((candidate) => candidate.kind === "save" && candidate.match_key === output.match_key);
          if (matching.length !== 1) { record({ status: "mismatch", message: "没有唯一对应的保存输出和主管线" }); return; }
          const outputNodeId = String(matching[0].node_id);
          if (String(item.outputNodeId || item.parsed?.normalized?.selected_output_node || "") === outputNodeId) { record({ status: "already", message: "已选择相同保存输出" }); return; }
          try {
            const parsed = await inspectImportOutput(item, outputNodeId, context);
            changed = batchEntryChanged(job, entry);
            if (changed) { record({ status: "skipped", message: changed }); return; }
            item.outputNodeId = outputNodeId;
            applyImportMetadata(item, parsed); item.status = "ready"; item.error = "";
            item.revision = (item.revision || 0) + 1;
            record({ status: "applied", message: item.editedFields.size ? "输出已更新；手动填写内容已保留，请核对是否适用于该分支" : "已解析该图片的对应保存输出", manualReview: !!item.editedFields.size });
          } catch (error) { record({ status: "failed", message: errorMessage(error, "保存输出读取失败，已保留原内容") }); }
        };
        for (let index = 0; index < job.entries.length; index += 3) {
          await Promise.all(job.entries.slice(index, index + 3).map(processEntry));
          job.loaded = Math.min(index + 3, job.entries.length); context.render();
        }
      } finally { finishImportBatch(job, context); }
    }

    function showImportBatchResult() {
      const result = importBatchResult;
      if (!result) return;
      const labels = { applied: "已应用", already: "无需重复", mismatch: "节点不匹配", conflict: "快照冲突", protected: "保留手动修改", failed: "读取失败", skipped: "已跳过" };
      void openModal(result.title, `<p class="field-hint">${escape(batchResultSummary(result))}</p><div class="import-batch-results">${result.results.map((entry) => `<div class="import-batch-result"><strong>${escape(entry.filename)}</strong><span>${escape(labels[entry.status] || entry.status)}</span><p>${escape(entry.message)}</p></div>`).join("")}</div>`, [{ label: "关闭", action: () => true }]);
    }

    function applyImportMetadata(item, parsed) {
      item.parsed = parsed;
      const normalized = parsed.normalized || {};
      const fields = { generation_engine: normalized.generation_engine || (own(ENGINES, parsed.format) ? parsed.format : "unknown"), prompt: normalized.prompt ?? "", negative_prompt: normalized.negative_prompt ?? "", model: normalized.model ?? "", mode: normalized.mode || "unknown", parameters: JSON.stringify(normalized.parameters || {}, null, 2), generated_at: localDateTime(normalized.generated_at ?? item.importedAt) };
      for (const [key, value] of Object.entries(fields)) if (!item.editedFields.has(key)) item.fields[key] = value;
    }

    async function inspectImportOutput(item, outputNodeId, context) {
      if (!context.editing) return apiPost("imports/inspect", { metadata: item.rawMetadata || item.parsed?.raw || {}, width: item.width, height: item.height, output_node_id: outputNodeId });
      const parsed = await apiGet(`gallery/import-edit/${context.generationId}`, { image_id: item.imageId, item_revision: item.itemRevision, output_node_id: outputNodeId });
      return { ...parsed, raw: item.rawMetadata || item.parsed?.raw || {} };
    }

    async function chooseImportOutput(item, outputNodeId, context = importContext) {
      if (!item || cardsBusy(context) || item.status === "reading") return;
      item.status = "reading"; item.error = ""; context.render();
      try {
        await context.invalidate();
        if (!context.active() || !context.items.includes(item)) return;
        const parsed = await inspectImportOutput(item, outputNodeId, context);
        if (!context.active() || !context.items.includes(item)) return;
        item.outputNodeId = outputNodeId;
        applyImportMetadata(item, parsed); item.status = "ready";
        if (item.editedFields.size) showNotice("输出分支已更新，手动填写的内容已保留，请核对。", "success");
      } catch (error) { item.status = "error"; item.error = errorMessage(error, "保存输出读取失败，已保留上次选择"); }
      if (context.active() && context.items.includes(item)) context.render();
    }

    function renderImportCards(context) {
      const grid = context.grid();
      if (!grid || !context.active()) return;
      context.sorter?.cancel();
      const expanded = new Set(Array.from(grid.querySelectorAll("[data-import-id] details[open]"), (entry) => {
        const card = entry.closest("[data-import-id]");
        return `${card.dataset.importId}:${Array.from(card.querySelectorAll("details")).indexOf(entry)}`;
      }));
      const focused = document.activeElement;
      const focusId = grid.contains(focused) ? focused?.closest?.("[data-import-id]")?.dataset.importId : null;
      const focusField = focused?.dataset?.importField;
      const focusSort = focused?.hasAttribute?.("data-sort-handle");
      const selection = focusField && typeof focused.selectionStart === "number" ? [focused.selectionStart, focused.selectionEnd] : null;
      grid.innerHTML = context.items.map((item, index) => importCard(item, index, context)).join("");
      grid.querySelectorAll("[data-import-id]").forEach((card) => card.querySelectorAll("details").forEach((entry, index) => { entry.open = expanded.has(`${card.dataset.importId}:${index}`); }));
      window.ImageStudioSelect?.refresh(grid);
      if (focusId && (focusField || focusSort)) {
        const restored = grid.querySelector(`[data-import-id="${focusId}"] ${focusSort ? "[data-sort-handle]" : `[data-import-field="${focusField}"]`}`);
        restored?.focus({ preventScroll: true }); if (selection && restored?.setSelectionRange) restored.setSelectionRange(...selection);
      }
    }

    function bindImportCards(context) {
      const grid = context.grid();
      context.sorter = window.ImageStudioSortable.bind(grid, {
        itemSelector: "[data-import-id]", handleSelector: "[data-sort-handle]",
        getId: (element) => element.dataset.importId, isEnabled: () => sortEnabled(context),
        onDragEnd: () => { if (context.editing && context.active()) context.render(); },
        onReorder: (ids) => {
          if (!sortEnabled(context) || ids.length !== context.items.length || new Set(ids).size !== ids.length) return;
          const byId = new Map(context.items.map((item) => [item.id, item]));
          if (ids.some((id) => !byId.has(id))) return;
          context.items = ids.map((id) => byId.get(id));
          context.batchResult = null;
          if (!context.editing) $("importProgress").textContent = "";
          void context.invalidate(); context.render();
        },
      });
      grid.addEventListener("click", (event) => {
        const retry = event.target.closest("[data-import-load]");
        if (retry && context.editing) {
          const item = context.items.find((entry) => entry.id === retry.closest("[data-import-id]")?.dataset.importId);
          if (item) { item.materialized = true; void ensureImportEditorItem(context, item); }
        }
        const remove = event.target.closest("[data-remove-import]"); if (remove && !context.editing) removeImport(remove.dataset.removeImport);
        const candidate = event.target.closest("[data-candidate-target]"); if (candidate) addPromptCandidate(candidate, context);
        const batchCandidate = event.target.closest("[data-batch-candidate-target]"); if (batchCandidate) void applyCandidateToImports(batchCandidate, context);
        const batchOutput = event.target.closest("[data-batch-import-output]"); if (batchOutput) void applyOutputToImports(batchOutput, context);
      });
      if (context.editing) grid.addEventListener("toggle", (event) => {
        const section = event.target;
        if (!section.open || !section.dataset.importDeferred) return;
        const item = context.items.find((entry) => entry.id === section.closest("[data-import-id]")?.dataset.importId);
        if (!item?.hydrated) return;
        const kind = section.dataset.importDeferred;
        delete section.dataset.importDeferred;
        if (kind === "parameters") section.insertAdjacentHTML("beforeend", `<textarea data-import-field="parameters" rows="5" spellcheck="false" aria-label="补充参数 JSON">${escape(item.fields.parameters)}</textarea>`);
        else if (kind === "stage" || kind === "conditions") {
          const value = kind === "stage" ? { ...(item.parsed.normalized.stages || [])[Number(section.dataset.stageIndex)] } : { condition_nodes: item.parsed.normalized.condition_nodes };
          delete value.node_id; delete value.type;
          section.insertAdjacentHTML("beforeend", `<div class="detail-parameter-grid">${Object.entries(value).map(([key, value]) => `<div class="detail-parameter-row"><div class="detail-parameter-label"><span>${escape(key)}</span></div><pre>${escape(serial(value))}</pre></div>`).join("")}</div>`);
        }
        else {
          const template = document.createElement("template");
          template.innerHTML = kind === "candidates" ? promptCandidatesMarkup(item, context, true) : comfyDetailsMarkup(item.parsed, false, true);
          const content = template.content.firstElementChild;
          while (content?.children.length > 1) section.append(content.children[1]);
        }
      }, true);
      grid.addEventListener("change", (event) => {
        if (!event.target.matches("[data-import-output]")) return;
        const item = context.items.find((entry) => entry.id === event.target.closest("[data-import-id]").dataset.importId);
        void chooseImportOutput(item, event.target.value, context);
      });
      grid.addEventListener("input", (event) => {
        const key = event.target.dataset.importField; if (!key || context.saving) return;
        const card = event.target.closest("[data-import-id]");
        const item = context.items.find((entry) => entry.id === card.dataset.importId); if (!item) return;
        void context.invalidate();
        if (["prompt", "negative_prompt"].includes(key)) updatePromptField(item, key, event.target.value);
        else item.fields[key] = event.target.value;
        item.editedFields.add(key); item.revision = (item.revision || 0) + 1;
        if (["prompt", "negative_prompt"].includes(key)) syncCandidateButtons(card, item, context);
      });
    }

    function renderImports() {
      renderImportCards(importContext);
      $("importDropzone").classList.toggle("is-compact", imports.length > 0);
      $("importDropzoneLabel").textContent = imports.length ? "继续添加图片" : "添加图片";
      $("importSummary").textContent = importBatchJob ? `正在批量应用选择… ${importBatchJob.entries.length} 张` : importAddJobs ? `正在校验图片… 已选择 ${imports.length} 张` : imports.length ? `已选择 ${imports.length} 张图片` : "尚未选择图片";
      const batchSummary = $("importBatchSummary");
      if (batchSummary) {
        batchSummary.classList.toggle("is-hidden", !importBatchResult || !!importBatchJob);
        batchSummary.querySelector("span").textContent = importBatchResult ? batchResultSummary(importBatchResult) : "";
      }
      $("confirmImportButton").disabled = importing || !!importBatchJob || importAddJobs > 0 || !imports.length || imports.some((item) => item.status === "reading");
      $("cancelImportButton").disabled = importing; $("importDropzone").disabled = importing; $("importFiles").disabled = importing;
      $("importGroupOption").classList.toggle("is-hidden", imports.length < 2);
      $("importAsGroup").disabled = importing || !!importBatchJob || imports.length < 2;
      if (imports.length < 2) $("importAsGroup").checked = false;
      $("importMergeOption").classList.toggle("is-hidden", !imports.length);
      $("importMergeExisting").disabled = importing || !!importBatchJob || !imports.length;
      if (!imports.length) $("importMergeExisting").checked = false;
      setCommandLabel("confirmImportButton", importing ? "正在导入…" : "确认导入");
    }

    function parseImportParameters(value, filename) {
      let parameters;
      try { parameters = value.trim() ? JSON.parse(value) : {}; } catch { throw new Error(`${filename} 的补充参数不是合法 JSON。`); }
      if (!parameters || typeof parameters !== "object" || Array.isArray(parameters)) throw new Error(`${filename} 的补充参数必须是 JSON 对象。`);
      const checkNumbers = (entry) => {
        if (typeof entry === "number" && (!Number.isFinite(entry) || (Number.isInteger(entry) && !Number.isSafeInteger(entry)))) throw new Error(`${filename} 包含超出网页安全范围的数值，请将大整数写成带双引号的字符串。`);
        if (entry && typeof entry === "object") Object.values(entry).forEach(checkNumbers);
      };
      checkNumbers(parameters);
      return parameters;
    }

    function renderImportEditor(context) {
      if (!context.active()) return;
      renderImportEditorCards(context);
      const busy = context.loading || !!context.loadError || cardsBusy(context) || context.items.some((item) => item.status === "reading");
      $("importEditSave").disabled = busy;
      $("importEditStatus").textContent = context.loading ? "正在读取图片列表…" : context.loadError ? context.loadError : context.saving ? "正在保存…" : context.batchJob ? `正在批量应用选择… ${context.batchJob.loaded} / ${context.batchJob.entries.length} 张` : `${context.items.length} 张图片 · 滚动读取参数，可拖动手柄或按方向键排序`;
      $("importEditRetry").hidden = !context.loadError;
      $("importEditLoading").hidden = !context.loading;
      const result = context.batchResult;
      const summary = $("importEditBatchSummary");
      summary.hidden = !result || !!context.batchJob;
      if (summary._result !== result) {
        summary._result = result;
        summary.innerHTML = result ? `<summary>${escape(batchResultSummary(result))} · 查看结果</summary><div class="import-batch-results">${result.results.map((entry) => `<div class="import-batch-result"><strong>${escape(entry.filename)}</strong><p>${escape(entry.message)}</p></div>`).join("")}</div>` : "";
      }
    }

    function importEditorPlaceholder(item, index, context) {
      return `<article class="import-card import-card-placeholder glass" data-import-id="${item.id}" data-import-sha256="${item.sha256}"><div class="import-card-header"><button class="studio-icon-button import-sort-handle" data-sort-handle type="button" aria-label="调整第 ${index + 1} 张图片顺序：${escape(item.file.name)}" data-tooltip="拖动排序，也可聚焦后使用方向键" ${sortEnabled(context) ? "" : "disabled"}>${icon("GripVertical")}</button><span class="import-order">${index + 1}</span><strong data-tooltip="${escape(item.file.name)}" data-tooltip-overflow>${escape(item.file.name)}</strong></div><div class="import-card-preview import-preview-placeholder" data-sort-surface>${icon("Image")}</div><div class="import-file-meta">${formatBytes(item.file.size)}${item.width ? ` · ${item.width} × ${item.height}` : ""}</div><div class="import-fields-placeholder" aria-hidden="true"><span></span><span></span><span></span></div><p class="import-card-status" role="status"></p><button class="quiet-button" data-import-load type="button">读取参数</button></article>`;
    }

    function renderImportEditorCards(context) {
      const grid = context.grid();
      if (!grid || !context.active()) return;
      if (context.sorter?.isDragging?.()) return;
      const sorting = sortEnabled(context);
      context.items.forEach((item, index) => {
        let card = context.nodes.get(item.id);
        const full = !!(item.hydrated && item.materialized);
        if (!card || card._full !== full || full && card._parsed !== item.parsed) {
          const template = document.createElement("template");
          template.innerHTML = full ? importCard(item, index, context) : importEditorPlaceholder(item, index, context);
          const replacement = template.content.firstElementChild;
          // A card is replaced only when its own data first arrives or is reparsed.
          // Unrelated drafts, focus, expanded sections, and DOM nodes stay intact.
          if (card) { context.observer?.unobserve(card); card.replaceWith(replacement); }
          else grid.append(replacement);
          card = replacement; card._full = full; card._parsed = item.parsed;
          context.nodes.set(item.id, card);
          if (full) window.ImageStudioSelect?.refresh(card);
          else context.observer?.observe(card);
        }
        if (grid.children[index] !== card) grid.insertBefore(card, grid.children[index] || null);
        const renderState = [index, sorting, cardsBusy(context), item.revision, item.status, item.hydrating, item.error, item.parsed, full];
        if (card._renderState?.every((value, position) => value === renderState[position])) return;
        card._renderState = renderState;
        const handle = card.querySelector("[data-sort-handle]");
        handle.disabled = !sorting;
        handle.setAttribute("aria-label", `调整第 ${index + 1} 张图片顺序：${item.file.name}`);
        const order = card.querySelector(".import-order"); order.textContent = index + 1; order.setAttribute("aria-label", `第 ${index + 1} 张`);
        const status = card.querySelector(".import-card-status");
        if (full) {
          card.querySelector("fieldset").disabled = cardsBusy(context) || item.status === "reading";
          card.querySelectorAll("[data-import-field]").forEach((field) => { if (field.value !== String(item.fields[field.dataset.importField] ?? "")) field.value = item.fields[field.dataset.importField] ?? ""; });
          syncCandidateButtons(card, item, context);
          card.querySelectorAll("[data-batch-candidate-target]").forEach((button) => { const candidate = item.parsed?.normalized?.prompt_candidates?.find((entry) => entry.id === button.dataset.batchCandidateId); button.disabled = batchChoiceDisabled(item, context) || !candidate?.match_key; });
          const output = card.querySelector("[data-batch-import-output]");
          if (output) output.disabled = batchChoiceDisabled(item, context) || !item.parsed?.normalized?.outputs?.some((entry) => entry.kind === "save" && String(entry.node_id) === String(item.outputNodeId) && entry.match_key);
          status.textContent = item.status === "reading" ? "正在识别图片参数…" : item.error || (item.parsed?.format && item.parsed.format !== "unknown" ? `已识别 ${engineLabel(item.parsed.format)}` : "未检测到生图参数，可手动填写");
        } else {
          status.textContent = item.error || (item.hydrating ? "正在读取图片参数…" : "滚动到此处时读取图片参数");
          const button = card.querySelector("[data-import-load]"); button.disabled = item.hydrating || context.saving; button.textContent = item.error ? "重试读取" : "读取参数";
          card.setAttribute("aria-busy", String(!!item.hydrating));
        }
        status.classList.toggle("is-error", !!item.error);
      });
    }

    function ensureImportEditorItem(context, item) {
      if (!context.active()) return Promise.resolve(false);
      if (item.hydrated) { context.render(); return Promise.resolve(true); }
      if (item.hydrationPromise) return item.hydrationPromise;
      item.hydrating = true; item.error = "";
      item.hydrationPromise = new Promise((resolve) => context.queue.push({ item, resolve }));
      pumpImportEditorItems(context); context.render();
      return item.hydrationPromise;
    }

    function pumpImportEditorItems(context) {
      while (context.active() && context.requests < 3 && context.queue.length) {
        const { item, resolve } = context.queue.shift(); context.requests++;
        void (async () => {
          let loaded = false;
          try {
            const entry = await readEditSnapshot(context, item);
            if (!context.active()) return;
            let preview = hooks.getImageMedia?.(editMediaItem(item), "preview") || entry.thumbnail_data_url || "";
            if (!preview && hooks.loadImageMedia) {
              try { preview = await hooks.loadImageMedia(editMediaItem(item), "preview"); }
              catch { /* Missing media must not hide otherwise editable metadata. */ }
            }
            if (!context.active()) return;
            const fields = { generation_engine: entry.fields.generation_engine || "unknown", model: entry.fields.model ?? "", mode: entry.fields.mode || "unknown", prompt: entry.fields.prompt ?? "", negative_prompt: entry.fields.negative_prompt ?? "", generated_at: localDateTime(entry.fields.generated_at), parameters: entry.parameters_json ?? JSON.stringify(entry.fields.parameters || {}, null, 2) };
            Object.assign(item, { url: preview, importedAt: entry.fields.generated_at, parsed: entry.metadata, rawMetadata: entry.metadata?.raw || {}, fields, initialFields: { ...fields }, editedFields: new Set(entry.edited_fields || []), outputNodeId: entry.output_node_id || "", initialOutputNodeId: String(entry.output_node_id || ""), status: "ready", hydrated: true });
            loaded = true;
          } catch (error) { if (context.active()) item.error = errorMessage(error, "图片参数读取失败，请重试"); }
          finally {
            item.hydrating = false; item.hydrationPromise = null; context.requests--; resolve(loaded);
            if (context.active()) { context.render(); pumpImportEditorItems(context); }
          }
        })();
      }
    }

    async function loadImportEditorManifest(context) {
      if (!context.active() || context.loading) return;
      context.loading = true; context.loadError = ""; context.render();
      try {
        const record = await apiGet(`gallery/import-edit/${context.generationId}`, { light: 1 });
        if (!context.active()) return;
        context.revision = record.revision;
        context.items = record.items.map((entry) => ({ id: `edit_${entry.image_id}`, imageId: entry.image_id, itemRevision: entry.item_revision, sha256: entry.sha256, thumbnailRevision: entry.thumbnail_revision, file: { name: entry.filename, size: entry.size_bytes }, width: entry.width, height: entry.height, fields: {}, initialFields: {}, editedFields: new Set(), status: "unloaded", hydrated: false, revision: 0 }));
        const validKeys = new Set(context.items.map((item) => editSnapshotKey(context, item)));
        for (const key of editSnapshots.keys()) if (key.startsWith(`${context.generationId}:`) && !validKeys.has(key)) removeEditSnapshot(key);
        context.loading = false; context.render();
        if (!context.observer) {
          const visible = () => {
            if (!context.active()) return;
            const bounds = $("studioModalBody").getBoundingClientRect();
            context.items.forEach((item) => { const card = context.nodes.get(item.id); const box = card.getBoundingClientRect(); if (!item.materialized && box.bottom >= bounds.top - 300 && box.top <= bounds.bottom + 300) { item.materialized = true; void ensureImportEditorItem(context, item); } });
          };
          $("studioModalBody").addEventListener("scroll", visible, { passive: true }); context.fallbackVisible = visible; visible();
        }
      } catch (error) { if (context.active()) context.loadError = errorMessage(error, "图片列表读取失败，请重试"); }
      finally { if (context.active()) { context.loading = false; context.render(); } }
    }

    async function saveImportEditor(context) {
      if (!context.active() || context.loading || context.loadError || cardsBusy(context) || context.items.some((item) => item.status === "reading")) return undefined;
      const items = context.items.map((item) => {
        if (!item.hydrated) return { image_id: item.imageId, overrides: {} };
        if (item.parsed?.normalized?.requires_output_selection) throw new Error(`${item.file.name} 包含多个保存输出，请先选择最终保存输出。`);
        const overrides = {};
        for (const key of item.editedFields) {
          // Compare displayed values, and do not serialize untouched JSON or dates.
          // Their originals may contain integers or timestamp precision beyond JS.
          if (!own(item.fields, key) || item.fields[key] === item.initialFields[key]) continue;
          if (key === "parameters") overrides[key] = parseImportParameters(item.fields[key], item.file.name);
          else if (key === "generated_at") {
            overrides[key] = item.fields[key] ? new Date(item.fields[key]).getTime() / 1000 : null;
            if (overrides[key] !== null && !Number.isFinite(overrides[key])) throw new Error(`${item.file.name} 的生成时间无效。`);
          } else overrides[key] = item.fields[key];
        }
        if (String(item.outputNodeId || "") !== item.initialOutputNodeId) overrides.comfy_output_node = item.outputNodeId;
        return { image_id: item.imageId, overrides };
      });
      context.saving = true; context.render();
      try { return await apiPost(`gallery/import-edit/${context.generationId}`, { revision: context.revision, items }); }
      finally { context.saving = false; context.render(); }
    }

    async function openImportEditor() {
      const detail = state.detailData;
      if (detail?.source !== "import" || importEditLoading || importEditor) return;
      const viewedImageId = detail.images?.[state.detailImageIndex]?.id;
      importEditLoading = true; updateDetailActions(detail);
      const context = {
        editing: true, generationId: detail.id, revision: "", loading: false, loadError: "",
        saving: false, addJobs: 0, epoch: 0, batchJob: null, batchResult: null, sorter: null,
        items: [], nodes: new Map(), queue: [], requests: 0, closed: false, observer: null,
        grid: () => $("importEditGrid"), active: () => importEditor === context && !context.closed,
        invalidate: async () => {}, render: () => renderImportEditor(context),
      };
      const dispose = () => {
        context.closed = true; context.epoch++; context.observer?.disconnect(); context.sorter?.destroy();
        if (context.fallbackVisible) $("studioModalBody").removeEventListener("scroll", context.fallbackVisible);
        for (const entry of context.queue.splice(0)) { entry.item.hydrating = false; entry.item.hydrationPromise = null; entry.resolve(false); }
      };
      importEditor = context;
      try {
        const saved = await openModal("编辑导入记录", '<p class="field-hint" id="importEditStatus" role="status">正在读取图片列表…</p><button id="importEditRetry" class="quiet-button" type="button" hidden>重试读取列表</button><div id="importEditLoading" class="import-edit-loading" aria-hidden="true"><span></span><span></span><span></span></div><details id="importEditBatchSummary" class="import-edit-batch-summary" hidden></details><div id="importEditGrid" class="import-grid" aria-label="编辑已导入图片"></div>', [
          { label: "取消", id: "importEditCancel", action: () => false },
          { label: "保存", id: "importEditSave", primary: true, action: () => saveImportEditor(context) },
        ], { importEditor: true, dismissOutside: false, focus: "studioModalClose", onClose: dispose, onOpen: () => {
          bindImportCards(context);
          if (window.IntersectionObserver) context.observer = new IntersectionObserver((entries) => {
            if (!context.active()) return;
            for (const entry of entries) if (entry.isIntersecting) {
              const item = context.items.find((item) => item.id === entry.target.dataset.importId);
              if (item && !item.materialized) { item.materialized = true; void ensureImportEditorItem(context, item); }
            }
          }, { root: $("studioModalBody"), rootMargin: "300px 0px" });
          $("importEditRetry").addEventListener("click", () => void loadImportEditorManifest(context));
          void loadImportEditorManifest(context);
        } });
        importEditor = null;
        if (saved) {
          showNotice("导入记录已保存。", "success");
          await hooks.loadGallery(state.galleryPage);
          if (state.detailId === detail.id) await hooks.openDetail(detail.id, Math.max(0, saved.image_ids.indexOf(viewedImageId)), { resetScroll: false });
        }
      } catch (error) { showNotice(errorMessage(error, "导入记录读取失败"), "error"); }
      finally {
        dispose();
        if (importEditor === context) importEditor = null;
        importEditLoading = false; updateDetailActions(state.detailData);
      }
    }

    function removeImport(id) {
      if (importing) return;
      const item = imports.find((entry) => entry.id === id); if (!item) return;
      void discardImportGroup();
      URL.revokeObjectURL(item.url); imports = imports.filter((entry) => entry !== item); renderImports();
    }

    function clearImports() {
      if (importing) return;
      importEpoch++; importAddJobs = 0;
      importBatchJob = null; importBatchResult = null;
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
        item.rawMetadata = raw;
        applyImportMetadata(item, parsed);
        item.status = "ready";
      } catch (error) { item.status = "error"; item.error = errorMessage(error, "图片参数识别失败，可手动填写后导入"); }
      if (imports.includes(item)) renderImports();
    }

    function addImportFiles(files) {
      if (importing) return;
      const accepted = Array.from(files).filter((file) => /^image\/(png|jpeg|webp|gif)$/.test(file.type) || /\.(png|jpe?g|webp|gif)$/i.test(file.name));
      if (!accepted.length) { showNotice("请选择 PNG、JPEG、WebP 或 GIF 图片。", "error"); return; }
      hooks.switchView("import");
      const epoch = importEpoch;
      importAddJobs++; renderImports();
      // Serialize additions so simultaneous file picks cannot create duplicate cards.
      importAddQueue = importAddQueue.then(async () => {
        const added = []; let skipped = 0;
        for (const file of accepted) {
          if (epoch !== importEpoch) return;
          if (file.size > 30 * 1024 * 1024) { showNotice(`${file.name} 超过 30 MB。`, "error"); continue; }
          let sha256;
          try { sha256 = await window.ImageStudioHash.fileSHA256(file); }
          catch (error) { if (epoch === importEpoch) showNotice(errorMessage(error, `${file.name} 校验失败，请重新选择`), "error"); continue; }
          if (epoch !== importEpoch) return;
          if (imports.some((item) => item.sha256 === sha256)) { skipped++; continue; }
          if (imports.length >= 100) { showNotice("单次最多选择 100 张不同图片。", "error"); break; }
          if (!added.length) await discardImportGroup();
          if (epoch !== importEpoch) return;
          const importedAt = Date.now() / 1000;
          const item = { id: `import_${Date.now().toString(36)}_${++importSequence}`, sha256, file, url: URL.createObjectURL(file), importedAt, editedFields: new Set(), fields: { generation_engine: "unknown", prompt: "", negative_prompt: "", model: "", mode: "unknown", parameters: "{}", generated_at: localDateTime(importedAt) }, status: "reading" };
          imports.push(item); added.push(item); renderImports();
        }
        if (skipped) showNotice(`已忽略 ${skipped} 张待导入列表中的重复图片。`);
        for (let index = 0; index < added.length; index += 3) {
          if (epoch !== importEpoch) return;
          await Promise.all(added.slice(index, index + 3).filter((item) => imports.includes(item)).map(inspectFile));
        }
      }).catch((error) => { if (epoch === importEpoch) showNotice(errorMessage(error, "图片添加失败"), "error"); })
        .finally(() => { if (epoch === importEpoch) { importAddJobs--; renderImports(); } });
      return importAddQueue;
    }

    function localDateTime(timestamp) {
      if (timestamp == null || timestamp === "") return "";
      const date = new Date(Number(timestamp) * 1000);
      if (!Number.isFinite(date.getTime())) return "";
      const pad = (value) => String(value).padStart(2, "0");
      const milliseconds = date.getMilliseconds();
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${milliseconds ? `.${String(milliseconds).padStart(3, "0")}` : ""}`;
    }

    async function confirmImports() {
      if (importing || importBatchJob || importAddJobs || !imports.length || imports.some((item) => item.status === "reading")) return;
      let preparedItems;
      let mergeEngine = "";
      try {
        if ($("importMergeExisting").checked) {
          const sources = imports.map((item) => item.fields.generation_engine === "nai" ? "novelai" : item.fields.generation_engine);
          if (sources.some((source) => !source || ["unknown", "mixed"].includes(source))) throw new Error("合并前请先为每张图片选择明确的生图来源。");
          if (new Set(sources).size !== 1) throw new Error(`合并到已有图组时，生图来源必须相同：${imports.map((item, index) => `${item.file.name}：${engineLabel(sources[index])}`).join("；")}`);
          mergeEngine = sources[0];
        }
        preparedItems = imports.map((item) => {
          if (item.parsed?.normalized?.requires_output_selection) throw new Error(`${item.file.name} 包含多个保存输出，请先选择最终保存输出。`);
          const parameters = parseImportParameters(item.fields.parameters, item.file.name);
          const overrides = {};
          if (item.outputNodeId) overrides.comfy_output_node = item.outputNodeId;
          for (const key of item.editedFields) overrides[key] = key === "parameters" ? parameters : item.fields[key];
          if (item.editedFields.has("generated_at") || item.parsed?.normalized?.generated_at == null) overrides.generated_at = item.fields.generated_at ? new Date(item.fields.generated_at).getTime() / 1000 : null;
          if ($("importAsGroup").checked) overrides.model = item.fields.model;
          return { client_id: item.id, sha256: item.sha256, filename: item.file.name, overrides };
        });
        if ($("importAsGroup").checked) {
          const missing = preparedItems.filter((item) => !String(item.overrides.model || "").trim());
          if (missing.length) throw new Error(`作为图组导入时，请先填写这些图片的模型：${missing.map((item) => item.filename).join("、")}`);
          if (new Set(preparedItems.map((item) => String(item.overrides.model).trim())).size !== 1) throw new Error(`图组中的模型必须相同，当前模型不一致：${preparedItems.map((item) => `${item.filename}：${item.overrides.model}`).join("；")}`);
        }
      } catch (error) { showNotice(error.message, "error"); return; }
      importing = true; renderImports();
      try {
        let groupOptions = mergeEngine ? { merge_target_id: importGroupDraft?.targetId, generation_engine: mergeEngine } : { as_group: $("importAsGroup").checked };
        const retry = importGroupDraft?.signature === JSON.stringify({ items: preparedItems, ...groupOptions });
        if (!retry) {
          await discardImportGroup();
          imports.forEach((item) => { item.duplicateReason = ""; item.error = ""; });
          $("importProgress").textContent = "正在检查画廊中已有的图片…";
          renderImports();
          requireImportAllowed(await apiPost("imports/check", { items: preparedItems.map(({ client_id, sha256 }) => ({ client_id, sha256 })) }));
          if (mergeEngine) {
            $("importProgress").textContent = "正在查找同源的已导入图组…";
            const targetId = await chooseImportMergeTarget(mergeEngine);
            $("importProgress").textContent = "";
            if (!targetId) return;
            groupOptions = { merge_target_id: targetId, generation_engine: mergeEngine };
          }
        }
        await confirmImportGroup(preparedItems, groupOptions);
      } catch (error) {
        if (error.discardImportBatch) await discardImportGroup();
        const message = errorMessage(error, "图片导入失败，请重试");
        $("importProgress").textContent = message; showNotice(message, "error");
      }
      finally { importing = false; renderImports(); }
    }

    function requireImportAllowed(result) {
      if (result?.allowed !== false) return result;
      const hashes = new Set(result.duplicate_hashes || []);
      for (const item of imports) {
        if (hashes.has(item.sha256)) item.duplicateReason = result.code === "batch_duplicates" ? "本批图片重复" : "画廊中已存在";
        if (result.code === "hash_mismatch" && result.client_id === item.id) { item.status = "error"; item.error = result.message; }
      }
      renderImports();
      const error = new Error(result.message || "本批图片未通过校验，导入已取消。");
      error.discardImportBatch = ["gallery_duplicates", "batch_duplicates"].includes(result.code);
      throw error;
    }

    async function discardImportGroup() {
      const draft = importGroupDraft; importGroupDraft = null;
      if (draft) await apiPost(draft.prepared.cancel_endpoint, {}).catch(() => {});
    }

    async function chooseImportMergeTarget(engine) {
      const limit = 12;
      const getPage = (offset) => apiGet("imports/merge-targets", { generation_engine: engine, limit, offset, sort: getGallerySort?.() || "created" });
      let page = await getPage(0);
      if (!page.total) {
        showNotice("暂无同源的已导入图组，请选择“作为图组导入”；单张图片可关闭合并后直接导入。", "error");
        return null;
      }
      let offset = 0;
      let selected = null;
      let loading = false;
      let active = true;
      const previousTarget = importGroupDraft?.targetId;
      function renderPage() {
        $("importMergeTargets").innerHTML = page.items.map((item) => {
          const full = Number(item.image_count) + imports.length > 100;
          const checked = !full && item.id === selected?.id;
          return `<label class="merge-target-card ${full ? "is-unavailable" : ""}" data-merge-target="${escape(item.id)}"><div class="merge-target-preview">${item.thumbnail_data_url ? `<img src="${escape(item.thumbnail_data_url)}" alt="" />` : icon("Image")}<span class="merge-target-count">${Number(item.image_count)} 张</span></div><div class="merge-target-heading"><input type="radio" name="importMergeTarget" value="${escape(item.id)}" aria-label="选择图组：${escape(item.model || "未记录模型")}，${Number(item.image_count)} 张，${escape(formatDate(item.created_at))}" ${checked ? "checked" : ""} ${full || loading ? "disabled" : ""} /><strong>${escape(item.model || "未记录模型")}</strong></div><time>${escape(formatDate(item.created_at))}</time><span class="merge-target-prompt">${escape(item.prompt_preview || "未记录提示词")}</span>${full ? '<span class="merge-target-warning">合并后超过 100 张上限</span>' : ""}</label>`;
        }).join("");
        $("importMergePage").textContent = `第 ${Math.floor(offset / limit) + 1} / ${Math.max(1, Math.ceil(page.total / limit))} 页`;
        $("importMergePrev").disabled = loading || offset === 0;
        $("importMergeNext").disabled = loading || offset + limit >= page.total;
        $("importMergeConfirm").disabled = loading || !selected;
        $("importMergeSelection").textContent = selected ? `已选：${selected.model || "未记录模型"} · ${formatDate(selected.created_at)} · ${selected.image_count} 张` : "尚未选择图组";
      }
      async function changePage(nextOffset) {
        if (loading) return;
        loading = true; renderPage();
        $("importMergeTargets").setAttribute("aria-busy", "true");
        $("studioModalError").textContent = "";
        try {
          const next = await getPage(nextOffset);
          if (!active) return;
          page = next; offset = nextOffset;
        } catch (error) { if (active) $("studioModalError").textContent = errorMessage(error, "图组读取失败，请重试"); }
        finally {
          loading = false;
          if (active) { renderPage(); $("importMergeTargets").removeAttribute("aria-busy"); $("studioModalBody").scrollTop = 0; }
        }
      }
      return openModal("选择已有图组", `<p>${escape(engineLabel(engine))} · 待合并 ${imports.length} 张图片</p><div class="merge-target-grid" id="importMergeTargets" role="radiogroup" aria-label="已有导入图组"></div><div class="merge-target-pagination"><button type="button" class="studio-icon-button" id="importMergePrev" aria-label="上一页" data-tooltip="上一页">${icon("ChevronLeft")}</button><span id="importMergePage" aria-live="polite"></span><button type="button" class="studio-icon-button" id="importMergeNext" aria-label="下一页" data-tooltip="下一页">${icon("ChevronRight")}</button></div><div class="merge-target-selection" id="importMergeSelection" role="status"></div>`, [
        { label: "取消", action: () => false },
        { label: "确认导入", primary: true, id: "importMergeConfirm", action: () => {
          if (loading) return undefined;
          if (!selected) throw new Error("请先选择一个图组。");
          return selected.id;
        } },
      ], { mergePicker: true, focus: "studioModalClose", onOpen: () => {
        selected = page.items.find((item) => item.id === previousTarget && Number(item.image_count) + imports.length <= 100) || null;
        renderPage();
        $("importMergeTargets").addEventListener("change", (event) => {
          if (loading || !event.target.matches('input[name="importMergeTarget"]')) return;
          selected = page.items.find((item) => item.id === event.target.value) || null;
          $("importMergeConfirm").disabled = !selected;
          $("importMergeSelection").textContent = selected ? `已选：${selected.model || "未记录模型"} · ${formatDate(selected.created_at)} · ${selected.image_count} 张` : "尚未选择图组";
        });
        $("importMergePrev").addEventListener("click", () => void changePage(Math.max(0, offset - limit)));
        $("importMergeNext").addEventListener("click", () => void changePage(offset + limit));
      } }).finally(() => { active = false; });
    }

    async function confirmImportGroup(items, groupOptions = { as_group: true }) {
      const signature = JSON.stringify({ items, ...groupOptions });
      if (importGroupDraft?.signature !== signature) {
        await discardImportGroup();
        const prepared = requireImportAllowed(await apiPost("imports/prepare", { items, ...groupOptions }));
        importGroupDraft = { signature, prepared, targetId: groupOptions.merge_target_id, uploaded: new Set() };
      }
      const draft = importGroupDraft;
      const client = await bridge();
      for (let index = 0; index < imports.length; index++) {
        const item = imports[index];
        if (draft.uploaded.has(item.id)) continue;
        const ticket = draft.prepared.items.find((entry) => entry.client_id === item.id);
        $("importProgress").textContent = `正在上传图片 ${index + 1} / ${imports.length}`;
        try {
          requireImportAllowed(await client.upload(ticket.upload_endpoint, item.file));
          draft.uploaded.add(item.id); item.status = "ready"; item.error = "已上传，等待整批入库";
        } catch (error) {
          item.status = "error"; item.error = errorMessage(error, "上传失败，请重试"); renderImports();
          if (/过期|取消/.test(item.error)) await discardImportGroup();
          throw error;
        }
        renderImports();
      }
      $("importProgress").textContent = "正在保存本批图片…";
      try {
        requireImportAllowed(await apiPost(draft.prepared.commit_endpoint, {}));
      } catch (error) {
        if (/过期|取消|目标.*(?:删除|不存在|来源|数量|100 张)/.test(errorMessage(error, ""))) await discardImportGroup();
        throw error;
      }
      const count = imports.length;
      imports.forEach((item) => URL.revokeObjectURL(item.url)); imports = [];
      importGroupDraft = null;
      const message = groupOptions.merge_target_id ? `已合并 ${count} 张图片到已有图组` : groupOptions.as_group ? `已导入 1 个图组，共 ${count} 张图片` : `已导入 ${count} 张图片`;
      $("importProgress").textContent = message;
      showNotice(`${message}。`, "success");
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

    function promptStatusMarkup(status, prefix = "") {
      const label = { summary: "组合或多阶段文本摘要", partial: "部分解析", missing: "未读取到文本" }[status];
      return label ? `<span class="comfy-summary-status">${escape(prefix)}${label}</span>` : "";
    }

    function deferredDetailSection(className, title, content) {
      const index = detailDeferred.push(content) - 1;
      return `<details class="${className}" data-detail-deferred="${index}"><summary>${title}</summary></details>`;
    }

    function comfyDetailsMarkup(metadata, withCopy = false, lazy = false) {
      if (metadata?.format !== "comfyui") return "";
      const normalized = metadata.normalized || {}; const stages = normalized.stages || [];
      if (!stages.length) return "";
      const rows = (values) => withCopy ? parameterRows(values) : Object.entries(values).map(([key, value]) => `<div class="detail-parameter-row"><div class="detail-parameter-label"><span>${escape(key)}</span></div><pre>${escape(serial(value))}</pre></div>`).join("");
      const outputs = (normalized.outputs || []).map((entry) => `<span>${entry.kind === "save" ? "保存输出" : "预览输出"} #${escape(entry.node_id)}${String(entry.node_id) === String(normalized.selected_output_node) ? " · 摘要分支" : ""} · 阶段 ${(entry.stage_ids || []).map(escape).join("、") || "无"}</span>`).join("");
      const stageMarkup = stages.map((stage, index) => {
        const fields = { ...stage }; delete fields.node_id; delete fields.type;
        for (const key of ["prompt_status", "negative_prompt_status"]) if (fields[key]) fields[key] = ({ exact: "直接文本", summary: "可读摘要，非等价条件", partial: "部分解析", missing: "未读取到文本" })[fields[key]] || fields[key];
        const title = `阶段 ${index + 1} · ${escape(stage.type)} #${escape(stage.node_id)}`;
        if (withCopy) return deferredDetailSection("comfy-stage", title, () => `<div class="detail-parameter-grid">${rows(fields)}</div>`).replace("<details ", `<details data-comfy-stage="${escape(stage.node_id)}" `);
        return `<details class="comfy-stage" data-comfy-stage="${escape(stage.node_id)}" ${lazy ? `data-import-deferred="stage" data-stage-index="${index}"` : ""}><summary>${title}</summary>${lazy ? "" : `<div class="detail-parameter-grid">${rows(fields)}</div>`}</details>`;
      }).join("");
      const conditions = normalized.condition_nodes && Object.keys(normalized.condition_nodes).length ? withCopy ? deferredDetailSection("comfy-conditions", "条件组合结构", () => rows({ condition_nodes: normalized.condition_nodes })) : `<details class="comfy-conditions" ${lazy ? 'data-import-deferred="conditions"' : ""}><summary>条件组合结构</summary>${lazy ? "" : rows({ condition_nodes: normalized.condition_nodes })}</details>` : "";
      return `<details class="comfy-workflow-info"><summary>采样阶段与条件 · ${stages.length} 个阶段</summary><div class="comfy-output-list">${outputs}</div>${stageMarkup}${conditions}</details>`;
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
      const comfyReproduction = engine === "comfyui" && (providerKind === "comfyui" || !!image?.metadata?.raw?.prompt);
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
      $("detailImportEdit").disabled = importEditLoading || !detail || !(detail.images || []).length;
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
      $("importFiles").addEventListener("change", (event) => { void addImportFiles(event.target.files); event.target.value = ""; });
      $("importDropzone").addEventListener("click", () => $("importFiles").click());
      $("confirmImportButton").addEventListener("click", () => void confirmImports());
      $("cancelImportButton").addEventListener("click", clearImports);
      for (const [id, other] of [["importAsGroup", "importMergeExisting"], ["importMergeExisting", "importAsGroup"]]) {
        $(id).addEventListener("change", () => { if ($(id).checked) $(other).checked = false; void discardImportGroup(); });
      }
      const batchSummary = document.createElement("div"); batchSummary.id = "importBatchSummary"; batchSummary.className = "import-batch-summary is-hidden";
      batchSummary.innerHTML = '<span role="status"></span><button class="quiet-button" type="button">查看结果</button>';
      batchSummary.querySelector("button").addEventListener("click", showImportBatchResult);
      $("importGrid").before(batchSummary);
      bindImportCards(importContext);
      for (const view of [$("galleryView"), $("importView")]) {
        view.addEventListener("dragover", (event) => { if (!Array.from(event.dataTransfer.types).includes("Files")) return; event.preventDefault(); view.classList.add("is-drop-target"); });
        view.addEventListener("dragleave", (event) => { if (!view.contains(event.relatedTarget)) view.classList.remove("is-drop-target"); });
        view.addEventListener("drop", (event) => { event.preventDefault(); view.classList.remove("is-drop-target"); void addImportFiles(event.dataTransfer.files); });
      }
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
      $("detailImportEdit").addEventListener("click", () => void openImportEditor());
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
      $("studioModalClose").addEventListener("click", () => modalClose?.(false));
      $("studioModalRoot").querySelector(".studio-modal-scrim").addEventListener("click", () => { if (modalDismissOutside) modalClose?.(false); });
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
        const focusable = Array.from(modal.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')).filter((item) => !item.matches(".studio-select-native") && !item.closest("[inert]") && item.getClientRects().length);
        if (!focusable.length) { event.preventDefault(); modal.focus(); return; }
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
      }, true);
      window.addEventListener("beforeunload", () => imports.forEach((item) => URL.revokeObjectURL(item.url)));
    }

    return { bind, modeLabel, engineLabel, galleryPageSize, closeGalleryPagePicker, renderGalleryCard, galleryRendered, selectionChanged, syncFloatingBars, detailMetadataMarkup, detailWarningsMarkup, layoutDetailParameters, layoutSettingsPanels, clearDetailParameterLayout, updateDetailActions, copyText, resolveParameters, schemaPolicyButton, editParameterPolicy, setCommandLabel, openModal, modalOpen: () => !!modalClose };
  };
  window.ImageStudioMetadata = { extractMetadata, decodeComment };
})();
