(function () {
  "use strict";

  window.__imageStudioAppLoaded = true;

  const state = {
    view: "generate", mode: "text2img", providers: [], models: [], selectedProviderId: "", selectedModelRef: "", defaultModelRefs: { text2img: "", img2img: "" }, parameterValues: {}, references: [],
    resultImages: [], galleryItems: [], selectedIds: new Set(), settings: null, selectedSettingsProviderId: "", selectedSettingsModelId: "", modelEditorTab: "model", editingToolParameter: "", editingToolDefaultChoices: [], detailId: "",
  };
  let activeConfirmation = null;
  let settingsLoadPromise = null;
  let eventsBound = false;
  const MODEL_DEFAULT_CHOICE = "__model_default__";
  const $ = (id) => document.getElementById(id);
  const els = {
    pageTitle: $("pageTitle"), pageSubtitle: $("pageSubtitle"), runtimeStatus: $("runtimeStatus"), providerStatus: $("providerStatus"),
    modelChoice: $("modelChoice"), modelProvider: $("modelProvider"), workspaceEmpty: $("workspaceEmpty"), generatorWorkspace: $("generatorWorkspace"), modelParameters: $("modelParameters"), referenceField: $("referenceField"), referenceUpload: $("referenceUpload"), referenceStrip: $("referenceStrip"),
    generationForm: $("generationForm"), prompt: $("prompt"), negativePromptField: $("negativePromptField"), negativePrompt: $("negativePrompt"), negativePromptHint: $("negativePromptHint"), resetNegativePromptButton: $("resetNegativePromptButton"), advancedParameters: $("advancedParameters"), parameters: $("parameters"), generationError: $("generationError"), generateButton: $("generateButton"), resultEmpty: $("resultEmpty"), resultGrid: $("resultGrid"), resultMeta: $("resultMeta"),
    galleryGrid: $("galleryGrid"), galleryEmpty: $("galleryEmpty"), gallerySearch: $("gallerySearch"), galleryProvider: $("galleryProvider"), galleryMode: $("galleryMode"), gallerySource: $("gallerySource"), selectionBar: $("selectionBar"), selectionCount: $("selectionCount"),
    detailDrawer: $("detailDrawer"), drawerBody: $("drawerBody"), detailDate: $("detailDate"), scrim: $("scrim"), imagePreview: $("imagePreview"), previewImage: $("previewImage"), imagePreviewTitle: $("imagePreviewTitle"), downloadImageButton: $("downloadImageButton"),
    settingTool: $("settingTool"), settingPageDefaultTextModel: $("settingPageDefaultTextModel"), settingPageDefaultImageModel: $("settingPageDefaultImageModel"), settingToolDefaultTextModel: $("settingToolDefaultTextModel"), settingToolDefaultImageModel: $("settingToolDefaultImageModel"), historyEnabled: $("historyEnabled"), retainReferences: $("retainReferences"), recordInvocationIdentity: $("recordInvocationIdentity"), historyRecords: $("historyRecords"), historyMegabytes: $("historyMegabytes"), settingsProviderList: $("settingsProviderList"), providerForm: $("providerForm"), settingsModelList: $("settingsModelList"), modelForm: $("modelForm"), settingsError: $("settingsError"), addProviderButton: $("addProviderButton"), addModelButton: $("addModelButton"), newModelChoice: $("newModelChoice"), newModelChoices: $("newModelChoices"), saveSettingsButton: $("saveSettingsButton"), parameterDialog: $("parameterDialog"), toolParameterExposed: $("toolParameterExposed"), toolParameterDescription: $("toolParameterDescription"), toolParameterDefault: $("toolParameterDefault"), toolParameterDefaultChoice: $("toolParameterDefaultChoice"), toolParameterDefaultHint: $("toolParameterDefaultHint"), toolParameterChoices: $("toolParameterChoices"),
  };

  async function bridge() {
    const deadline = Date.now() + 5000;
    while (!window.AstrBotPluginPage && Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 25));
    }
    if (!window.AstrBotPluginPage) throw new Error("AstrBot 页面桥接尚未加载，请刷新页面后重试。");
    const client = window.AstrBotPluginPage;
    let timeoutId = null;
    try {
      await Promise.race([
        client.ready(),
        new Promise((_, reject) => { timeoutId = window.setTimeout(() => reject(new Error("AstrBot 页面通信超时，请重新打开插件页面。")), 8000); }),
      ]);
    } finally { window.clearTimeout(timeoutId); }
    return client;
  }
  async function apiGet(path, params) { return (await bridge()).apiGet(path, params); }
  async function apiPost(path, body) { return (await bridge()).apiPost(path, body); }

  function referenceLimitForModel(model) { return model?.supports_img2img ? Math.max(0, Math.min(8, Number(model.max_reference_images || 0))) : 0; }
  function modelsForMode() { return state.models.filter((item) => state.mode === "text2img" ? item.supports_text2img : referenceLimitForModel(item) > 0); }
  function selectedModel() { return state.models.find((item) => item.model_ref === state.selectedModelRef) || null; }
  function selectedProvider() { const model = selectedModel(); return state.providers.find((item) => item.id === (model?.provider_id || state.selectedProviderId)) || null; }
  function text(value) { return value === null || value === undefined ? "" : String(value); }
  function escape(value) { const div = document.createElement("div"); div.textContent = text(value); return div.innerHTML; }
  function formatDate(value) { return new Date(Number(value) * 1000).toLocaleString(); }
  function formatBytes(value) { const bytes = Number(value || 0); return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(0, Math.round(bytes / 1024))} KB`; }
  function sourceLabel(value) { return ({ webui: "WebUI", command: "指令", llm_tool: "LLM 工具" })[value] || value || "未知"; }
  function invocationSourceLabel(source) { if (!source || !Object.values(source).some((value) => value)) return "未记录"; return { 场景: source.context_type === "group" ? "群聊" : source.context_type === "private" ? "私聊" : source.context_type, 平台: source.platform_name, 平台实例: source.platform_id, 群ID: source.group_id, 群名称: source.group_name, 用户ID: source.user_id, 用户昵称: source.user_name }; }
  function setError(target, message) { target.textContent = message || ""; }
  function errorMessage(error, fallback) {
    const message = error instanceof Error ? error.message : String(error || "");
    if (!message) return fallback;
    if (/[\u3400-\u9fff]/.test(message)) return message;
    if (/network error|failed to fetch/i.test(message)) return `${fallback}：无法连接 AstrBot 后端`;
    if (/request failed with status code/i.test(message)) return `${fallback}：服务请求失败`;
    if (/plugin bridge/i.test(message)) return `${fallback}：页面通信失败`;
    return `${fallback}：${message}`;
  }
  function showNotice(message, tone = "info") {
    window.__showImageStudioNotice(message, tone);
  }

  function switchView(view) {
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
    document.querySelectorAll(".view").forEach((item) => item.classList.toggle("is-active", item.id === `${view}View`));
    const labels = { generate: ["生图", "选择模式和模型后开始创作"], gallery: ["画廊", "搜索、筛选、复现或导出历史生成记录"], settings: ["设置", "管理运行策略、历史、生图服务商和模型"], };
    els.pageTitle.textContent = labels[view][0]; els.pageSubtitle.textContent = labels[view][1];
    if (view === "gallery") void loadGallery();
    if (view === "settings") void loadSettings();
  }

  function collectModelParameters() {
    const values = {};
    els.modelParameters.querySelectorAll("[data-model-parameter]").forEach((input) => {
      if (input.dataset.uiOnly === "true") return;
      const key = input.dataset.modelParameter;
      if (input.type === "checkbox") values[key] = input.checked;
      else if (input.dataset.parameterType === "number") values[key] = input.value === "" ? "" : Number(input.value);
      else if (input.dataset.parameterType === "json") { try { values[key] = input.value.trim() ? JSON.parse(input.value) : {}; } catch { values[key] = input.value; } }
      else values[key] = input.value;
    });
    return values;
  }

  function renderModelParameter(name, descriptor) {
    const type = String(descriptor.type || "text").toLowerCase();
    const label = escape(name);
    const description = escape(descriptor.description || descriptor.label || name);
    let value = state.parameterValues[name] ?? descriptor.default ?? "";
    const requestKey = escape(descriptor.request_key || name);
    if (type === "preset" && Array.isArray(descriptor.choices)) {
      const options = descriptor.choices.map((choice) => `<option value="${escape(choice.value)}" ${String(choice.value) === String(value) ? "selected" : ""}>${escape(choice.label || choice.value)}</option>`).join("");
      return `<div class="field"><label title="${description}">${label}</label><select data-model-parameter="${escape(name)}" data-parameter-type="preset" data-preset-target="${escape(descriptor.target || "")}" data-ui-only="true">${options}</select></div>`;
    }
    if (type === "select" && Array.isArray(descriptor.choices)) {
      if (!descriptor.choices.some((choice) => String(typeof choice === "object" ? choice.value : choice) === String(value))) value = descriptor.default ?? descriptor.choices[0] ?? "";
      const options = descriptor.choices.map((choice) => { const option = typeof choice === "object" ? choice : { value: choice, label: choice }; return `<option value="${escape(option.value)}" ${String(option.value) === String(value) ? "selected" : ""}>${escape(option.label)}</option>`; }).join("");
      return `<div class="field"><label title="${description}">${label}</label><select data-model-parameter="${escape(name)}" data-parameter-type="select" data-request-key="${requestKey}">${options}</select></div>`;
    }
    if (type === "boolean" || type === "bool") return `<div class="toggle-row" title="${description}"><label>${label}</label><label class="toggle-control"><input data-model-parameter="${escape(name)}" data-request-key="${requestKey}" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`;
    if (type === "json" || type === "object") return `<div class="field field-wide"><label title="${description}">${label}</label><textarea data-model-parameter="${escape(name)}" data-parameter-type="json" data-request-key="${requestKey}" rows="3" spellcheck="false">${escape(typeof value === "string" ? value : JSON.stringify(value || {}, null, 2))}</textarea></div>`;
    if (type === "textarea") return `<div class="field field-wide"><label title="${description}">${label}</label><textarea data-model-parameter="${escape(name)}" data-parameter-type="text" data-request-key="${requestKey}" rows="4">${escape(value)}</textarea></div>`;
    const inputType = type === "number" || type === "int" || type === "float" ? "number" : "text";
    const min = descriptor.min !== undefined ? ` min="${escape(descriptor.min)}"` : "";
    const max = descriptor.max !== undefined ? ` max="${escape(descriptor.max)}"` : "";
    const step = descriptor.step !== undefined ? ` step="${escape(descriptor.step)}"` : inputType === "number" ? " step=\"any\"" : "";
    return `<div class="field"><label title="${description}">${label}</label><input data-model-parameter="${escape(name)}" data-parameter-type="${inputType === "number" ? "number" : "text"}" data-request-key="${requestKey}" type="${inputType}" value="${escape(value)}"${min}${max}${step} /></div>`;
  }

  function renderModelChoices() {
    const available = modelsForMode();
    if (!available.some((item) => item.model_ref === state.selectedModelRef)) state.selectedModelRef = "";
    els.modelChoice.innerHTML = available.length ? `<option value="">请选择模型</option>${available.map((item) => `<option value="${escape(item.model_ref)}" ${item.model_ref === state.selectedModelRef ? "selected" : ""}>${escape(item.name)} · ${escape(item.provider_name)}</option>`).join("")}` : '<option value="">当前模式没有可用模型</option>';
    els.modelChoice.disabled = available.length === 0;
    els.modelChoice.value = state.selectedModelRef;
    renderModelWorkspace();
  }

  function renderGenerationForm() {
    const model = selectedModel();
    const provider = selectedProvider();
    const supportsRefs = state.mode === "img2img" && referenceLimitForModel(model) > 0;
    const supportsNegative = !!model?.supports_negative_prompt;
    els.referenceField.classList.toggle("is-hidden", !supportsRefs);
    els.negativePromptField.classList.toggle("is-hidden", !supportsNegative);
    els.advancedParameters.classList.toggle("is-hidden", model?.provider_kind !== "custom_json");
    els.negativePrompt.disabled = !supportsNegative;
    els.negativePrompt.placeholder = "可选";
    els.negativePromptHint.textContent = supportsNegative ? "当前模型会将此字段作为专用反向提示词发送。" : "";
    els.providerStatus.textContent = model && provider ? `${model.name} · ${provider.name}` : "未选择模型";
    renderReferences();
  }

  function renderModelWorkspace() {
    const model = selectedModel();
    els.generatorWorkspace.disabled = !model;
    els.workspaceEmpty.classList.toggle("is-hidden", !!model);
    els.generatorWorkspace.classList.toggle("is-hidden", !model);
    if (!model) {
      els.modelParameters.innerHTML = "";
      els.modelProvider.textContent = "";
      renderGenerationForm();
      return;
    }
    els.modelProvider.textContent = model.provider_name || "";
    els.modelParameters.innerHTML = Object.entries(model.parameters || {}).map(([name, descriptor]) => renderModelParameter(name, descriptor)).join("") || '<div class="workspace-placeholder">该模型没有额外参数。</div>';
    els.modelParameters.querySelectorAll("[data-model-parameter]").forEach((input) => {
      const update = () => {
        state.parameterValues[input.dataset.modelParameter] = input.type === "checkbox" ? input.checked : input.value;
        if (input.dataset.presetTarget) applyParameterPreset(input.dataset.modelParameter, input.value);
        else syncParameterPresets(input.dataset.modelParameter);
      };
      input.addEventListener("input", update); input.addEventListener("change", update);
    });
    els.modelParameters.querySelectorAll("[data-preset-target]").forEach((input) => {
      const targetName = input.dataset.presetTarget;
      if (Object.prototype.hasOwnProperty.call(state.parameterValues, targetName)) syncParameterPresets(targetName);
      else applyParameterPreset(input.dataset.modelParameter, input.value);
    });
    renderGenerationForm();
  }

  function modelParameterInput(name) { return Array.from(els.modelParameters.querySelectorAll("[data-model-parameter]")).find((input) => input.dataset.modelParameter === name) || null; }
  function applyParameterPreset(presetName, choiceValue) {
    const descriptor = selectedModel()?.parameters?.[presetName];
    const choice = descriptor?.choices?.find((item) => String(item.value) === String(choiceValue));
    if (!choice || typeof choice.fill !== "string" || !descriptor.target) return;
    const targetInput = modelParameterInput(descriptor.target); if (!targetInput) return;
    targetInput.value = choice.fill; state.parameterValues[descriptor.target] = choice.fill;
  }
  function syncParameterPresets(targetName) {
    const model = selectedModel(); if (!model) return;
    const targetInput = modelParameterInput(targetName); if (!targetInput) return;
    Object.entries(model.parameters || {}).forEach(([name, descriptor]) => {
      if (String(descriptor.type || "").toLowerCase() !== "preset" || descriptor.target !== targetName) return;
      const presetInput = modelParameterInput(name); if (!presetInput) return;
      const matched = (descriptor.choices || []).find((choice) => typeof choice.fill === "string" && choice.fill === targetInput.value);
      const custom = (descriptor.choices || []).find((choice) => choice.value === "custom");
      presetInput.value = matched?.value || custom?.value || ""; state.parameterValues[name] = presetInput.value;
    });
  }

  function parameterValuesForModel(model, values) {
    const source = values && typeof values === "object" ? values : {};
    const result = {};
    Object.entries(model?.parameters || {}).forEach(([name, descriptor]) => {
      if (Object.prototype.hasOwnProperty.call(source, name)) result[name] = source[name];
      else if (descriptor?.request_key && Object.prototype.hasOwnProperty.call(source, descriptor.request_key)) result[name] = source[descriptor.request_key];
    });
    return result;
  }

  function renderReferences() {
    els.referenceStrip.innerHTML = state.references.map((item, index) => `<div class="reference-item"><img src="${item.preview_data_url}" alt="参考图 ${index + 1}" /><button type="button" data-reference-index="${index}" aria-label="移除参考图">×</button></div>`).join("");
    els.referenceStrip.querySelectorAll("[data-reference-index]").forEach((button) => button.addEventListener("click", () => { state.references.splice(Number(button.dataset.referenceIndex), 1); renderReferences(); }));
  }

  async function bootstrap() {
    const payload = await apiGet("studio/bootstrap");
    state.providers = Array.isArray(payload.providers) ? payload.providers : [];
    state.models = Array.isArray(payload.models) ? payload.models : [];
    state.parameterValues = {};
    state.defaultModelRefs = { text2img: payload.defaults?.text2img_model_ref || "", img2img: payload.defaults?.img2img_model_ref || "" };
    state.selectedModelRef = state.defaultModelRefs[state.mode] || "";
    els.negativePrompt.value = selectedModel()?.negative_prompt_default || "";
    els.runtimeStatus.textContent = `已加载 ${state.providers.length} 个生图服务商`;
    renderModelChoices();
  }

  async function uploadReferences(files) {
    const model = selectedModel();
    const maximum = referenceLimitForModel(model);
    const available = Math.max(0, maximum - state.references.length);
    if (available <= 0) { showNotice("当前模型没有可用的参考图名额。", "error"); return; }
    const client = await bridge();
    for (const file of Array.from(files).slice(0, available)) {
      const uploaded = await client.upload("studio/reference/upload", file);
      state.references.push(uploaded);
    }
    renderReferences();
  }

  async function generate(event) {
    event.preventDefault(); setError(els.generationError, "");
    let parameters = {};
    if (els.parameters.value.trim()) {
      try { parameters = JSON.parse(els.parameters.value); } catch { setError(els.generationError, "高级参数必须是合法 JSON"); return; }
    }
    const model = selectedModel();
    const provider = selectedProvider();
    if (!model || !provider) { setError(els.generationError, "请先选择支持当前模式的模型"); return; }
    if (state.mode === "img2img" && !state.references.length) { setError(els.generationError, "图生图需要至少一张参考图"); return; }
    const modelParameters = collectModelParameters();
    const size = modelParameters.size || "";
    const count = modelParameters.count || 1;
    delete modelParameters.size;
    delete modelParameters.count;
    const schema = model.parameters || {};
    const mappedParameters = Object.fromEntries(Object.entries(modelParameters).map(([name, value]) => [schema[name]?.request_key || name, value]));
    els.generateButton.disabled = true; els.generateButton.textContent = "生成中";
    try {
      const result = await apiPost("studio/generate", { mode: state.mode, provider_id: provider.id, model_ref: model.model_ref, prompt: els.prompt.value, negative_prompt: els.negativePrompt.value, model: model.id, size, count: Number(count), parameters: { ...parameters, ...mappedParameters }, reference_ids: state.references.map((item) => item.id) });
      state.resultImages = result.images || []; state.references = []; renderReferences();
      els.resultEmpty.classList.toggle("is-hidden", state.resultImages.length > 0); els.resultGrid.innerHTML = state.resultImages.map((image, index) => `<div class="result-card"><div class="result-frame"><img class="result-image-backdrop" src="${image.data_url}" alt="" aria-hidden="true" /><img class="result-image" src="${image.data_url}" alt="生成结果" data-result-preview="${index}" /></div><div class="result-card-actions"><button class="quiet-button" data-result-reference="${index}" type="button">用作参考图</button></div></div>`).join("");
      els.resultGrid.querySelectorAll("[data-result-reference]").forEach((button) => button.addEventListener("click", () => void useDataUrlAsReference(state.resultImages[Number(button.dataset.resultReference)].data_url, "generated-reference.png")));
      els.resultGrid.querySelectorAll("[data-result-preview]").forEach((image) => image.addEventListener("click", () => openImagePreview(image.src, `生成结果-${Number(image.dataset.resultPreview) + 1}`)));
      els.resultMeta.textContent = `${result.provider_name} · ${result.model} · ${(result.elapsed_ms / 1000).toFixed(1)} 秒${result.generation_id ? " · 已保存到画廊" : " · 历史未保留"}`;
    } catch (error) { setError(els.generationError, errorMessage(error, "生成失败")); }
    finally { els.generateButton.disabled = false; els.generateButton.textContent = "生成图片"; }
  }

  async function loadGallery() {
    try {
      const payload = await apiGet("gallery/list", { query: els.gallerySearch.value, provider_id: els.galleryProvider.value, mode: els.galleryMode.value, source: els.gallerySource.value });
      state.galleryItems = payload.items || []; state.selectedIds.clear(); renderGallery(payload);
    } catch (error) {
      showNotice(errorMessage(error, "画廊加载失败"), "error");
    }
  }

  function renderGallery(payload) {
    const currentProvider = els.galleryProvider.value; els.galleryProvider.innerHTML = '<option value="">全部服务商</option>' + (payload.filters?.providers || []).map((item) => `<option value="${escape(item.id)}">${escape(item.name)}</option>`).join(""); els.galleryProvider.value = currentProvider;
    els.galleryEmpty.classList.toggle("is-hidden", state.galleryItems.length > 0); els.galleryGrid.innerHTML = state.galleryItems.map((item) => `<article class="gallery-card" data-gallery-id="${item.id}"><div class="gallery-image-wrap"><img src="${item.thumbnail_data_url}" alt="${escape(item.prompt_preview)}" /><input type="checkbox" data-select-id="${item.id}" aria-label="选择生成记录" /></div><div class="gallery-info"><strong>${escape(item.provider_name)} · ${escape(item.model)}</strong><p>${escape(item.prompt_preview)}</p><div class="gallery-meta"><span>${item.mode === "img2img" ? "图生图" : "文生图"}</span><span>${formatDate(item.created_at)}</span></div></div></article>`).join("");
    els.galleryGrid.querySelectorAll("[data-gallery-id]").forEach((card) => card.addEventListener("click", (event) => { if (event.target.matches("input")) return; void openDetail(card.dataset.galleryId); }));
    els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => input.addEventListener("change", () => { input.checked ? state.selectedIds.add(input.dataset.selectId) : state.selectedIds.delete(input.dataset.selectId); updateSelection(); }));
    updateSelection();
  }

  function updateSelection() { els.selectionBar.classList.toggle("is-hidden", state.selectedIds.size === 0); els.selectionCount.textContent = `已选 ${state.selectedIds.size} 项`; }
  function clearGallerySelection() { state.selectedIds.clear(); els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => { input.checked = false; }); updateSelection(); }

  function requestParameters(detail) {
    const parameters = detail?.parameters && typeof detail.parameters === "object" ? detail.parameters : {};
    return { mode: detail?.mode || "", model: detail?.model || "", prompt: detail?.original_prompt || "", negative_prompt: parameters.negative_prompt || "", size: parameters.size || "", count: parameters.count || 1, parameters: parameters.parameters || {} };
  }

  function renderDetail(detail, fallbackThumbnail = "") {
    const images = Array.isArray(detail.images) ? detail.images : [];
    const availableImages = images.filter((item) => item?.data_url);
    const displayImages = availableImages.length ? availableImages : (fallbackThumbnail ? [{ data_url: fallbackThumbnail, mime_type: "image/webp", size_bytes: 0 }] : []);
    const refs = Array.isArray(detail.references) ? detail.references : [];
    const totalBytes = images.reduce((sum, item) => sum + Number(item.size_bytes || 0), 0);
    els.detailDate.textContent = formatDate(detail.created_at);
    const sourceIdentity = invocationSourceLabel(detail.invocation_source);
    els.drawerBody.innerHTML = `${displayImages.length ? `<div class="detail-images">${displayImages.map((item, index) => item.data_url ? `<div class="detail-image-frame"><img class="detail-image-backdrop" src="${escape(item.data_url)}" alt="" aria-hidden="true" /><img class="detail-image" src="${escape(item.data_url)}" alt="生成结果 ${index + 1}" data-detail-image="${index}" /></div>` : "").join("")}</div>` : '<div class="detail-loading">正在读取生成图片…</div>'}<div class="detail-block"><h3>提示词</h3><pre>${escape(detail.original_prompt)}</pre></div><div class="detail-block"><h3>请求参数</h3><pre>${escape(JSON.stringify(requestParameters(detail), null, 2))}</pre></div><div class="detail-block"><h3>信息</h3><pre>${escape(JSON.stringify({ 服务商: detail.provider_name, 模型: detail.model, 模式: detail.mode === "img2img" ? "图生图" : "文生图", 来源: sourceLabel(detail.source), 调用来源身份: sourceIdentity, 生成时间: formatDate(detail.created_at), 图片数量: images.length, 文件大小: formatBytes(totalBytes), 耗时毫秒: detail.elapsed_ms }, null, 2))}</pre></div><div class="detail-block"><h3>参考图</h3><div class="detail-references">${refs.length ? refs.map((item) => item.available ? item.data_url ? `<article class="detail-reference"><img src="${escape(item.data_url)}" alt="${escape(item.filename)}" data-detail-reference="${item.id}" /><div>${escape(item.filename)}<br>${formatBytes(item.size_bytes)}</div><button class="danger-button" data-reference-delete="${item.id}" type="button">删除参考图</button></article>` : `<article class="detail-reference"><div>${escape(item.filename)}<br>参考图正在加载…</div></article>` : `<article class="detail-reference"><div>参考图已删除</div></article>`).join("") : "<span>该记录没有保留参考图</span>"}</div></div><div class="detail-block detail-actions"><button class="primary-button" data-reproduce="${detail.id}" type="button">复现参数</button><button class="quiet-button" data-copy-request="${detail.id}" type="button">复制请求参数</button>${images[0]?.data_url ? ' <button class="quiet-button" data-output-reference="1" type="button">将当前成图用作新参考图</button>' : '<span class="field-hint">高清图片仍在加载，请稍候。</span>'}</div>`;
    els.drawerBody.querySelectorAll("[data-detail-image]").forEach((image) => image.addEventListener("click", () => openImagePreview(image.src, `生成结果 ${Number(image.dataset.detailImage) + 1}`)));
    els.drawerBody.querySelectorAll("[data-detail-reference]").forEach((image) => image.addEventListener("click", () => openImagePreview(image.src, image.alt)));
    els.drawerBody.querySelectorAll("[data-reference-delete]").forEach((button) => button.addEventListener("click", async () => {
      if (!await confirmAction("删除此参考图？生成结果和参数不会删除。")) return;
      try { await apiPost("gallery/reference/delete", { reference_id: button.dataset.referenceDelete }); showNotice("参考图已删除。", "success"); await openDetail(detail.id); }
      catch (error) { showNotice(errorMessage(error, "参考图删除失败"), "error"); }
    }));
    els.drawerBody.querySelector("[data-reproduce]")?.addEventListener("click", () => void reproduce(detail.id));
    els.drawerBody.querySelector("[data-copy-request]")?.addEventListener("click", () => void copyRequestParameters(detail));
    els.drawerBody.querySelector("[data-output-reference]")?.addEventListener("click", () => void useDataUrlAsReference(images[0].data_url, "gallery-output-reference.png"));
  }

  async function loadDetailAssets(id, summary, fallbackThumbnail) {
    try {
      const assets = await apiGet(`gallery/assets/${id}`);
      if (state.detailId !== id) return;
      renderDetail({ ...summary, ...assets }, fallbackThumbnail);
    } catch (error) {
      if (state.detailId === id) showNotice(errorMessage(error, "高清图片加载失败"), "error");
    }
  }

  async function openDetail(id) {
    state.detailId = id;
    const card = state.galleryItems.find((item) => String(item.id) === String(id));
    els.detailDrawer.classList.add("is-open"); els.detailDrawer.setAttribute("aria-hidden", "false"); els.scrim.classList.remove("is-hidden"); els.detailDrawer.focus();
    els.detailDate.textContent = "";
    els.drawerBody.innerHTML = '<div class="detail-loading">正在读取生成详情…</div>';
    try {
      const summary = await apiGet(`gallery/detail/${id}`, { assets: "0" });
      if (state.detailId !== id) return;
      renderDetail(summary, card?.thumbnail_data_url || "");
      void loadDetailAssets(id, summary, card?.thumbnail_data_url || "");
    } catch (error) { if (state.detailId === id) showNotice(errorMessage(error, "生成详情加载失败"), "error"); }
  }

  function closeDetail() { state.detailId = ""; closeImagePreview(); els.detailDrawer.classList.remove("is-open"); els.detailDrawer.setAttribute("aria-hidden", "true"); if (!activeConfirmation) els.scrim.classList.add("is-hidden"); }

  function openImagePreview(dataUrl, title) {
    if (!dataUrl) return;
    const extension = ((dataUrl.match(/^data:image\/([^;]+)/) || [])[1] || "png").replace("jpeg", "jpg");
    els.previewImage.src = dataUrl; els.previewImage.alt = title; els.imagePreviewTitle.textContent = title; els.downloadImageButton.href = dataUrl; els.downloadImageButton.download = `${String(title || "image").replace(/[^\w\u3400-\u9fff-]+/g, "_")}.${extension}`; els.imagePreview.classList.remove("is-hidden");
  }

  function closeImagePreview() { els.imagePreview.classList.add("is-hidden"); els.previewImage.removeAttribute("src"); els.downloadImageButton.href = "#"; }

  async function copyRequestParameters(detail) {
    const content = JSON.stringify(requestParameters(detail), null, 2);
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(content);
      else throw new Error("clipboard unavailable");
      showNotice("请求参数已复制到剪贴板。", "success");
    } catch {
      const textarea = document.createElement("textarea"); textarea.value = content; textarea.style.position = "fixed"; textarea.style.opacity = "0"; document.body.appendChild(textarea); textarea.select();
      const copied = document.execCommand("copy"); textarea.remove();
      showNotice(copied ? "请求参数已复制到剪贴板。" : "当前浏览器不允许访问剪贴板，请手动复制参数。", copied ? "success" : "error");
    }
  }

  async function reproduce(id) {
    try {
      const draft = await apiPost(`gallery/reproduce/${id}`, {}); state.mode = draft.mode || "text2img"; state.selectedProviderId = draft.provider_id || ""; state.references = draft.references || [];
      state.selectedModelRef = draft.model_ref || (draft.provider_id && draft.model ? `${draft.provider_id}:${draft.model}` : "");
      const rawParameterValues = { ...(draft.parameters || {}), size: draft.size || "", count: draft.count || 1 };
      state.parameterValues = rawParameterValues;
      els.prompt.value = draft.prompt || ""; els.negativePrompt.value = draft.negative_prompt || "";
      document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === state.mode)); renderModelChoices();
      const model = selectedModel();
      if (model) { state.parameterValues = parameterValuesForModel(model, rawParameterValues); renderModelWorkspace(); }
      closeDetail(); switchView("generate");
      if (draft.notice) setError(els.generationError, draft.notice);
    } catch (error) { showNotice(errorMessage(error, "复现参数读取失败"), "error"); }
  }

  function populateDefaultModelSelect(select, models, selectedRef) {
    select.innerHTML = `<option value="">未设置</option>${models.map((model) => `<option value="${escape(model.model_ref)}">${escape(model.name)} · ${escape(model.provider_name)}</option>`).join("")}`;
    select.value = models.some((model) => model.model_ref === selectedRef) ? selectedRef : "";
  }

  async function loadSettings() {
    if (settingsLoadPromise) return settingsLoadPromise;
    els.addProviderButton.disabled = true; els.addModelButton.disabled = true; els.saveSettingsButton.disabled = true;
    setError(els.settingsError, "正在读取设置…");
    settingsLoadPromise = (async () => {
      try {
        const payload = await apiGet("settings/get");
        if (!payload?.base || !payload?.webui || !Array.isArray(payload.webui.providers)) throw new Error("设置接口返回的数据格式无效");
        state.settings = payload;
        els.settingTool.checked = !!payload.base.enable_llm_tool;
        const history = payload.webui.history; els.historyEnabled.checked = !!history.enabled; els.retainReferences.checked = !!history.retain_reference_images; els.recordInvocationIdentity.checked = !!history.record_invocation_identity; els.historyRecords.value = history.max_records; els.historyMegabytes.value = history.max_megabytes;
        const defaults = payload.webui.generation_defaults || {}; const pageDefaults = defaults.page || {}; const toolDefaults = defaults.tool || {};
        const defaultModels = payload.webui.providers.filter((provider) => provider.enabled).flatMap((provider) => (provider.models || []).map((model) => ({ ...model, provider_name: provider.name, model_ref: `${provider.id}:${model.id}` })));
        const pageTextModels = defaultModels.filter((model) => model.supports_text2img); const pageImageModels = defaultModels.filter((model) => referenceLimitForModel(model) > 0);
        const toolModels = defaultModels.filter((model) => model.tool?.enabled !== false); const toolTextModels = toolModels.filter((model) => model.supports_text2img); const toolImageModels = toolModels.filter((model) => referenceLimitForModel(model) > 0 && Number(model.tool?.max_reference_images || 0) > 0);
        populateDefaultModelSelect(els.settingPageDefaultTextModel, pageTextModels, pageDefaults.text2img_model_ref || ""); populateDefaultModelSelect(els.settingPageDefaultImageModel, pageImageModels, pageDefaults.img2img_model_ref || ""); populateDefaultModelSelect(els.settingToolDefaultTextModel, toolTextModels, toolDefaults.text2img_model_ref || ""); populateDefaultModelSelect(els.settingToolDefaultImageModel, toolImageModels, toolDefaults.img2img_model_ref || "");
        if (!payload.webui.providers.some((item) => item.id === state.selectedSettingsProviderId)) state.selectedSettingsProviderId = payload.webui.providers[0]?.id || "";
        state.selectedSettingsModelId = "";
        renderSettingsProviders();
        const warnings = Array.isArray(payload.validation_errors) ? payload.validation_errors.filter(Boolean) : [];
        setError(els.settingsError, warnings.length ? `配置提示：${warnings.join("；")}` : "");
        return true;
      } catch (error) {
        state.settings = null;
        els.settingsProviderList.innerHTML = '<div class="provider-empty">设置加载失败，请点击“新增服务商”或“保存全部设置”重试。</div>';
        els.providerForm.innerHTML = "";
        const message = errorMessage(error, "设置加载失败");
        setError(els.settingsError, message); showNotice(message, "error");
        return false;
      } finally {
        els.addProviderButton.disabled = false; els.addModelButton.disabled = false; els.saveSettingsButton.disabled = false;
      }
    })();
    const loaded = await settingsLoadPromise;
    settingsLoadPromise = null;
    return loaded;
  }

  const PROVIDER_KINDS = [["openai_images", "OpenAI Images"], ["gemini", "Gemini 图片输出"], ["nai_direct", "NAI 第三方 GET（nai.sta1n.cn）"], ["custom_json", "自定义 JSON"]];
  const NAI_MODELS = [
    { id: "nai-diffusion-4-5-full", name: "NAI V4.5 完整版" },
    { id: "nai-diffusion-5-full", name: "NAI V5 完整版" },
  ];
  const PROVIDER_DEFAULTS = {
    openai_images: { base_url: "https://api.openai.com/v1", generate_path: "/images/generations", edit_path: "/images/edits", models_path: "/models", edit_request_format: "multipart", request_template: "", response_image_path: "", max_concurrent_generations: 2 },
    gemini: { base_url: "https://generativelanguage.googleapis.com", generate_path: "/v1beta/models/{model}:generateContent", edit_path: "/v1beta/models/{model}:generateContent", models_path: "/v1beta/models", edit_request_format: "json_data_url", request_template: "", response_image_path: "", max_concurrent_generations: 2 },
    nai_direct: { base_url: "https://nai.sta1n.cn", generate_path: "/generate", edit_path: "", models_path: "/models", edit_request_format: "json_data_url", request_template: "", response_image_path: "", max_concurrent_generations: 2 },
    custom_json: { base_url: "", generate_path: "/v1/images/generations", edit_path: "/v1/images/edits", models_path: "/models", edit_request_format: "json_data_url", request_template: "", response_image_path: "", max_concurrent_generations: 2 },
  };
  const NAI_ARTIST_PRESETS = {
    vertical: "[[[artist:dishwasher1910]]], {{yd_(orange_maru)}}, [artist:ciloranko], [artist:sho_(sho_lwlw)], [ningen mame], year 2024,",
    comicDoujin: "(masterpiece:1.3), (best quality:1.2), (highres), (absurdres),\n" +
      "(extremely detailed illustration:1.2), (anime style:1.1),\n\n" +
      "(artist:feipin zhanshi:1.0), (artist:nlebo-hentai:0.9), (artist:sos adult:0.85),\n" +
      "(artist:hews:0.4),\n\n(detailed skin texture:1.15), (glossy skin:1.1),\n" +
      "(thick lineart:1.1), (high contrast:1.15),\n(vivid colors:1.1), (detailed shading:1.15),\n" +
      "(warm color palette:1.05),\n(cute face:1.1), (detailed eyes:1.15), (detailed face:1.1),",
    r18: "0.9::misaka_12003-gou ::, dino_(dinoartforame), wanke, liduke, year 2025, realistic, 4k, -2::green ::, " +
      "textless version, The image is highly intricate finished drawn. Only the character's face is in anime style, but their body is in realistic style. " +
      "1.35::A highly finished photo-style artwork that has lively color, graphic texture, realistic skin surface, and lifelike flesh with little obliques::. " +
      "1.63::photorealistic::, 1.63::photo(medium)::, \n20::best quality, absurdres, very aesthetic, detailed, masterpiece::,, very aesthetic, masterpiece, no text,",
    lolita25d: "0.9::misaka_12003-gou & dino, rurudo,  mignon,wanke & liduk::, year 2025, realistic, 4k, -2::green ::, " +
      "textless version, The image is highly intricate finished drawn. Only the character's face is in anime style, but their body is in realistic style. " +
      "1.35::A highly finished photo-style artwork that has lively color, graphic texture, realistic skin surface, and lifelike flesh with little obliques::. " +
      "1.63::photorealistic::, 1.63::photo(medium)::, \n20::best quality, absurdres, very aesthetic, detailed, masterpiece::,, very aesthetic, masterpiece, no text,",
    anime: "1.4::asanagi::,{{{{{artist:asanagi}}}}},1.2::xiaoluo_xl::,1.3::Artist: misaka_12003-gou::," +
      "1.2::Artist:shexyo::,0.7::Artist:b.sa_(bbbs)::,1::Artist:qiandaiyiyu::,1.05::artist:natedecock::," +
      "1.05::artist:kunaboto::,0.75::artist:kandata_nijou::,1.05::artist:zer0.zer0 ::,1.05::artist:jasony::," +
      "0.75::misaka_12003-gou ::, dino_(dinoartforame), wanke, liduke, year 2025, realistic, 4k, -2::green ::, " +
      "{textless version, The image is highly intricate finished drawn,write realistically,true to life}, " +
      "1.35::A highly finished photo-style artwork that has lively color, graphic texture, realistic skin surface, and lifelike flesh with little obliques::, " +
      "1.63::photorealistic::,3::age slider::,1.63::photo(medium)::, 2::best quality, absurdres, very aesthetic, detailed, masterpiece::,-4::Muscle definition, abs::",
    galgame: "artist:ningen_mame,, noyu_(noyu23386566),, toosaka asagi,, location,\\n" +
      "20::best quality, absurdres, very aesthetic, detailed, masterpiece::,:,, very aesthetic, masterpiece, no text,",
  };
  const NAI_DEFAULT_NEGATIVE = "{{bad anatomy}},{bad feet},bad hands,{{{bad proportions}}},{blurry},cloned face,cropped,{{{deformed}}},{{{disfigured}}},error,{{{extra arms}}},{extra digit},{{{extra legs}}},extra limbs,{{extra limbs}},{fewer digits},{{{fused fingers}}},gross proportions,ink eyes,ink hair,jpeg artifacts,{{{{long neck}}}},low quality,{malformed limbs},{{missing arms}},{missing fingers},{{missing legs}},{{{more than 2 nipples}}},mutated hands,{{{mutation}}},normal quality,owres,{{poorly drawn face}},{{poorly drawn hands}},reen eyes,signature,text,{{too many fingers}},{{{ugly}}},username,uta,watermark,worst quality,{{{more than 2 legs}}},awkward hand sign,weird hand gesture,contorted hand,unnatural finger pose,deformed hand gesture,{shaka},{hang loose},{{rock on}},{shaka sign}";
  const MODEL_PRESETS = {
    openai_images: { size: { type: "select", label: "尺寸", default: "1024x1024", choices: ["1024x1024", "1536x1024", "1024x1536"], request_key: "size" }, count: { type: "number", label: "数量", default: 1, min: 1, max: 4, step: 1, request_key: "count" }, quality: { type: "select", label: "质量", default: "auto", choices: ["auto", "low", "medium", "high"], request_key: "quality" }, background: { type: "select", label: "背景", default: "auto", choices: ["auto", "transparent", "opaque"], request_key: "background" }, output_format: { type: "select", label: "输出格式", default: "png", choices: ["png", "jpeg", "webp"], request_key: "output_format" } },
    gemini: { aspect_ratio: { type: "select", label: "画面比例", default: "1:1", choices: ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"], request_key: "aspect_ratio" }, image_size: { type: "select", label: "图片尺寸", default: "1K", choices: ["1K", "2K", "4K"], request_key: "image_size" } },
    nai_direct: { style: { type: "preset", label: "绘画风格", description: "选择预设绘画风格；自定义会清空画师串。", default: "custom", target: "artist", ui_only: true, choices: [{ value: "vertical", label: "韩漫小清新风", fill: NAI_ARTIST_PRESETS.vertical }, { value: "comicDoujin", label: "漫画同人风", fill: NAI_ARTIST_PRESETS.comicDoujin }, { value: "r18", label: "2.5D唯美风", fill: NAI_ARTIST_PRESETS.r18 }, { value: "lolita25d", label: "2.5D唯美风（萝）", fill: NAI_ARTIST_PRESETS.lolita25d }, { value: "anime", label: "本子里番风", fill: NAI_ARTIST_PRESETS.anime }, { value: "galgame", label: "GalGame风", fill: NAI_ARTIST_PRESETS.galgame }, { value: "custom", label: "自定义", fill: "" }] }, artist: { type: "textarea", label: "画师串", description: "追加到提示词前方的画师与质量标签串。", default: "", request_key: "artist" }, size: { type: "select", label: "尺寸", description: "nai.sta1n.cn 使用的中文画幅与分辨率名称。", default: "竖图", choices: ["竖图", "横图", "方图", "2K竖图", "2K横图", "2K方图", "4K竖图", "4K横图", "4K方图"], request_key: "size" }, sampler: { type: "select", label: "采样器", description: "控制从噪声生成画面的采样方式。", default: "k_euler_ancestral", choices: ["k_dpmpp_2m_sde", "k_dpmpp_2m", "k_dpmpp_sde", "k_dpmpp_2s_ancestral", "k_euler_ancestral", "k_euler", "ddim"], request_key: "sampler" }, steps: { type: "number", label: "采样步数", description: "采样迭代次数；当前第三方接口路由限制为 1–28。", default: 24, min: 1, max: 28, step: 1, request_key: "steps" }, scale: { type: "number", label: "提示词引导强度", description: "数值越高越强调遵循提示词，过高可能产生不自然效果。", default: 6, min: 1, max: 20, step: 0.1, request_key: "scale" }, cfg: { type: "number", label: "CFG Rescale", description: "缓解高提示词引导造成的颜色过饱和；上游映射为 cfg_rescale。", default: 0.3, min: 0, max: 1, step: 0.05, request_key: "cfg" }, noise_schedule: { type: "select", label: "噪声调度", description: "控制采样过程中的噪声变化曲线。", default: "karras", choices: ["karras", "exponential", "polyexponential", "native"], request_key: "noise_schedule" } },
    custom_json: { size: { type: "text", label: "尺寸（可选）", default: "1024x1024", request_key: "size" }, count: { type: "number", label: "数量", default: 1, min: 1, max: 4, step: 1, request_key: "count" } },
  };
  function providerDefaults(kind) { return { ...(PROVIDER_DEFAULTS[kind] || PROVIDER_DEFAULTS.custom_json) }; }
  function modelPreset(kind) { return JSON.parse(JSON.stringify(MODEL_PRESETS[kind] || MODEL_PRESETS.custom_json)); }
  function currentSettingsProvider() { return state.settings?.webui.providers.find((item) => item.id === state.selectedSettingsProviderId) || null; }
  function renderSettingsProviders() {
    const providers = state.settings?.webui.providers || []; els.settingsProviderList.innerHTML = providers.length ? providers.map((item) => `<button class="provider-row ${item.id === state.selectedSettingsProviderId ? "is-active" : ""}" type="button" data-settings-provider="${escape(item.id)}"><strong>${escape(item.name || item.id)}</strong><span>${item.enabled ? "启用" : "停用"}</span></button>`).join("") : '<div class="provider-empty">尚未添加生图服务商</div>';
    els.settingsProviderList.querySelectorAll("[data-settings-provider]").forEach((button) => button.addEventListener("click", () => { state.selectedSettingsProviderId = button.dataset.settingsProvider; state.selectedSettingsModelId = ""; renderSettingsProviders(); }));
    renderProviderEditor();
    renderModelEditor();
  }
  function renderProviderEditor() {
    const provider = currentSettingsProvider(); if (!provider) { els.providerForm.innerHTML = '<div class="provider-empty">选择或新增生图服务商后编辑详细配置。</div>'; return; }
    const kind = provider.kind || "custom_json";
    const credentialField = kind === "nai_direct" ? `${field("api_key", "生图 Token（toUserId）", provider.api_key)}<div class="field"><span class="field-hint">填写在 nai.sta1n.cn 申请的 toUserId。</span></div>` : field("api_key", "接口密钥（API Key）", provider.api_key);
    const headersField = kind === "nai_direct" ? "" : textAreaField("custom_headers", "自定义请求头（JSON 或每行一个 Header）", provider.custom_headers);
    const common = `${field("id", "ID", provider.id)}${field("name", "名称", provider.name)}${selectField("kind", "供应类型", kind, PROVIDER_KINDS)}${field("base_url", "接口地址（Base URL）", provider.base_url)}${credentialField}${field("timeout_seconds", "超时秒数", provider.timeout_seconds, "number")}${field("max_concurrent_generations", "Provider 最大并发", provider.max_concurrent_generations ?? 2, "number")}${toggleField("enabled", "启用", provider.enabled)}${headersField}`;
    const typeFields = kind === "openai_images" ? `${field("generate_path", "文生图路径", provider.generate_path)}${field("edit_path", "图生图路径", provider.edit_path)}${field("models_path", "模型列表路径", provider.models_path || "/models")}${selectField("edit_request_format", "图生图请求格式", provider.edit_request_format, [["multipart", "multipart"], ["json_data_url", "JSON data URL"]])}` : kind === "gemini" ? `${field("generate_path", "generateContent 路径（支持 {model}）", provider.generate_path)}${field("models_path", "模型列表路径", provider.models_path || "/v1beta/models")}` : kind === "nai_direct" ? `${field("generate_path", "生成路径", provider.generate_path)}<div class="field field-wide"><span class="field-hint">第三方服务协议：GET /generate；Token 作为 token 查询参数发送。该类型不是 NovelAI 官方 API，且仅支持文生图。</span></div>` : `${field("generate_path", "文生图路径", provider.generate_path)}${field("edit_path", "图生图路径", provider.edit_path)}${field("models_path", "模型列表路径", provider.models_path || "/models")}${selectField("edit_request_format", "图生图请求格式", provider.edit_request_format, [["multipart", "multipart"], ["json_data_url", "JSON data URL"]])}${textAreaField("request_template", "请求 JSON 模板（可选）", provider.request_template)}${field("response_image_path", "响应图片路径（可选）", provider.response_image_path)}<div class="field field-wide"><span class="field-hint">模板可使用 {{prompt}}、{{model}}、{{size}}、{{count}} 和参数字段。</span></div>`;
    const discoveryButton = kind === "nai_direct" ? "" : '<button class="quiet-button" id="discoverModelsButton" type="button">获取模型</button>';
    els.providerForm.innerHTML = `<h3>${escape(provider.name || "生图服务商")}</h3>${common}${typeFields}<div class="provider-editor-actions"><button class="danger-button" id="removeProviderButton" type="button">删除服务商</button>${discoveryButton}</div>`;
    els.providerForm.querySelectorAll("[data-provider-field]").forEach((input) => input.addEventListener("input", () => updateProviderField(input))); els.providerForm.querySelectorAll("[data-provider-field]").forEach((input) => input.addEventListener("change", () => updateProviderField(input)));
    $("removeProviderButton")?.addEventListener("click", async () => { if (!await confirmAction("删除此生图服务商？历史记录不会删除。")) return; state.settings.webui.providers = state.settings.webui.providers.filter((item) => item.id !== provider.id); state.selectedSettingsProviderId = state.settings.webui.providers[0]?.id || ""; renderSettingsProviders(); showNotice("已从设置草稿中删除，保存全部设置后生效。", "success"); });
    $("discoverModelsButton")?.addEventListener("click", () => void discoverProviderModels(provider));
  }
  function field(key, label, value, type = "text") { return `<div class="field"><label>${label}</label><input data-provider-field="${key}" type="${type}" value="${escape(value)}" /></div>`; }
  function textAreaField(key, label, value) { return `<div class="field field-wide"><label>${label}</label><textarea data-provider-field="${key}" rows="3">${escape(value)}</textarea></div>`; }
  function selectField(key, label, value, options) { return `<div class="field"><label>${label}</label><select data-provider-field="${key}">${options.map(([id, name]) => `<option value="${id}" ${id === value ? "selected" : ""}>${name}</option>`).join("")}</select></div>`; }
  function toggleField(key, label, value) { return `<div class="toggle-row"><label>${label}</label><label class="toggle-control"><input data-provider-field="${key}" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`; }
  function updateProviderField(input) { const provider = currentSettingsProvider(); if (!provider) return; const key = input.dataset.providerField; const value = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value; if (key === "kind" && value !== provider.kind) { Object.assign(provider, providerDefaults(value)); provider.kind = value; state.selectedSettingsModelId = ""; renderProviderEditor(); renderModelEditor(); return; } provider[key] = value; if (key === "id") { state.selectedSettingsProviderId = input.value; const activeRow = els.settingsProviderList.querySelector(".provider-row.is-active"); if (activeRow) { activeRow.dataset.settingsProvider = input.value; if (!provider.name) activeRow.querySelector("strong").textContent = input.value; } } }
  async function discoverProviderModels(provider) { const button = $("discoverModelsButton"); if (button) { button.disabled = true; button.textContent = "获取中…"; } try { const payload = await apiPost("provider/models", { provider }); provider.discovered_models = payload.models || []; renderNewModelChoices(provider); showNotice(`已获取 ${provider.discovered_models.length} 个模型，可在新增模型时选择。`, "success"); } catch (error) { showNotice(errorMessage(error, "获取模型失败"), "error"); } finally { if (button) { button.disabled = false; button.textContent = "获取模型"; } } }
  function renderNewModelChoices(provider = currentSettingsProvider()) { const models = provider?.kind === "nai_direct" ? NAI_MODELS : provider?.discovered_models || []; els.newModelChoices.innerHTML = models.map((item) => `<option value="${escape(item.id)}">${escape(item.name || item.id)}${item.capability_source === "unknown" ? " · 能力未知" : ""}</option>`).join(""); els.newModelChoice.value = ""; els.newModelChoice.placeholder = provider?.kind === "nai_direct" ? "选择 NAI 模型或手动输入 ID" : "选择或输入模型 ID"; }

  function currentSettingsModel() { const provider = currentSettingsProvider(); return provider?.models?.find((item) => item.id === state.selectedSettingsModelId) || null; }
  function renderModelEditor() {
    const provider = currentSettingsProvider();
    renderNewModelChoices(provider);
    if (!provider) { els.settingsModelList.innerHTML = '<div class="provider-empty">请先选择服务商</div>'; els.modelForm.innerHTML = '<div class="provider-empty">选择服务商后配置模型能力。</div>'; return; }
    provider.models = Array.isArray(provider.models) ? provider.models : [];
    if (!provider.models.some((item) => item.id === state.selectedSettingsModelId)) state.selectedSettingsModelId = provider.models[0]?.id || "";
    els.settingsModelList.innerHTML = provider.models.length ? provider.models.map((item) => `<button class="provider-row ${item.id === state.selectedSettingsModelId ? "is-active" : ""}" type="button" data-settings-model="${escape(item.id)}"><strong>${escape(item.name || item.id)}</strong><span>${referenceLimitForModel(item) > 0 ? "图生图" : item.supports_text2img ? "文生图" : "未开放"}</span></button>`).join("") : '<div class="provider-empty">该服务商尚未添加模型</div>';
    els.settingsModelList.querySelectorAll("[data-settings-model]").forEach((button) => button.addEventListener("click", () => { state.selectedSettingsModelId = button.dataset.settingsModel; renderModelEditor(); }));
    const model = currentSettingsModel();
    if (!model) { els.modelForm.innerHTML = '<div class="provider-empty">点击“新增模型”开始配置。</div>'; return; }
    ensureToolConfig(model, provider);
    const tabs = `<div class="model-tabs"><button class="model-tab ${state.modelEditorTab === "model" ? "is-active" : ""}" data-model-tab="model" type="button">模型配置</button><button class="model-tab ${state.modelEditorTab === "tool" ? "is-active" : ""}" data-model-tab="tool" type="button">工具配置</button></div>`;
    els.modelForm.innerHTML = state.modelEditorTab === "tool" ? `${tabs}${renderToolConfiguration(model)}` : `${tabs}${renderModelConfiguration(provider, model)}`;
    els.modelForm.querySelectorAll("[data-model-tab]").forEach((button) => button.addEventListener("click", () => { state.modelEditorTab = button.dataset.modelTab; renderModelEditor(); }));
    bindModelConfiguration(provider, model);
  }
  function renderModelConfiguration(provider, model) {
    const schemaText = JSON.stringify(model.parameters || modelPreset(provider.kind), null, 2);
    const discoveredIds = (provider.discovered_models || []).map((item) => item.id); const modelChoices = provider.kind === "nai_direct" ? ["nai-diffusion-4-5-full", "nai-diffusion-5-full"] : discoveredIds;
    const modelIdField = modelChoices.length ? modelSelectField("id", "模型 ID", model.id, modelChoices, true) : modelField("id", "模型 ID", model.id);
    const negativeDefaultField = model.supports_negative_prompt ? modelTextAreaField("negative_prompt_default", "默认反向提示词", model.negative_prompt_default || "") : "";
    const testDisabled = !model.supports_text2img;
    const defaults = `<section class="schema-preview"><h4>参数默认值</h4>${Object.entries(model.parameters || {}).map(([name, descriptor]) => renderSchemaDefault(name, descriptor)).join("") || '<span class="field-hint">当前 schema 没有参数。</span>'}</section>`;
    const raw = `<details class="schema-raw"><summary>高级：参数 Schema</summary><textarea id="modelParametersSchema" data-model-field="parameters" rows="14" spellcheck="false">${escape(schemaText)}</textarea><span class="field-hint">每个字段支持 type、label、description、default、request_key、min、max、step、choices。</span></details>`;
    const capabilityEditable = ["unknown", "manual"].includes(model.capability_source);
    const capabilityHint = model.supports_img2img ? `<div class="field"><label>参考图能力上限</label><input data-model-field="max_reference_images" type="number" min="0" max="8" step="1" value="${Number(model.max_reference_images || 0)}"${capabilityEditable ? "" : " disabled"} /><span class="field-hint">${capabilityEditable ? "无法获取时可手动填写；0 表示不开放图生图，正数表示最多接受的参考图数量。" : `来源：${escape(model.capability_source)}，已获取的能力不可在此覆盖。`}</span></div>` : "";
    return `<h3>${escape(model.name || model.id)}</h3>${modelIdField}${modelField("name", "显示名称", model.name)}${modelToggle("supports_text2img", "支持文生图", model.supports_text2img)}${modelToggle("supports_img2img", "支持图生图", model.supports_img2img, provider.kind === "nai_direct")}${modelToggle("supports_negative_prompt", "支持专用反向提示词", model.supports_negative_prompt, provider.kind === "gemini")}${capabilityHint}${negativeDefaultField}${defaults}${raw}<div class="provider-editor-actions"><button class="danger-button" id="removeModelButton" type="button">删除模型</button><button class="quiet-button" id="testModelButton" type="button"${testDisabled ? ' disabled title="仅支持图生图的模型需要参考图，暂不能在此测试"' : ""}>测试模型</button></div>`;
  }
  function renderSchemaDefault(name, descriptor) { const type = String(descriptor.type || "text").toLowerCase(); const title = escape(descriptor.description || descriptor.label || name); const value = descriptor.default ?? ""; if ((type === "select" || type === "preset") && Array.isArray(descriptor.choices)) return `<div class="field"><label title="${title}">${escape(name)}</label><select data-schema-default="${escape(name)}">${descriptor.choices.map((choice) => { const item = typeof choice === "object" ? choice : { value: choice, label: choice }; return `<option value="${escape(item.value)}" ${String(item.value) === String(value) ? "selected" : ""}>${escape(item.label || item.value)}</option>`; }).join("")}</select></div>`; if (type === "boolean" || type === "bool") return `<div class="toggle-row" title="${title}"><label>${escape(name)}</label><label class="toggle-control"><input data-schema-default="${escape(name)}" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`; const inputType = ["number", "int", "integer", "float"].includes(type) ? "number" : "text"; return `<div class="field"><label title="${title}">${escape(name)}</label><input data-schema-default="${escape(name)}" type="${inputType}" value="${escape(value)}"${descriptor.min !== undefined ? ` min="${escape(descriptor.min)}"` : ""}${descriptor.max !== undefined ? ` max="${escape(descriptor.max)}"` : ""}${descriptor.step !== undefined ? ` step="${escape(descriptor.step)}"` : ""} /></div>`; }
  function renderToolConfiguration(model) { const tool = model.tool; const configuredLimit = referenceLimitForModel(model); const refLimit = model.supports_img2img ? `<div class="field"><label>LLM 最大参考图数量</label><input data-tool-field="max_reference_images" type="number" min="0" max="${configuredLimit}" value="${Math.min(configuredLimit, Number(tool.max_reference_images || 0))}"${configuredLimit > 0 ? "" : " disabled"} /><span class="field-hint">${configuredLimit > 0 ? `不能超过模型能力上限 ${configuredLimit}` : "模型参考图能力上限为 0，不向图生图及 LLM 图生图工具开放"}</span></div>` : ""; const rows = Object.entries(model.parameters || {}).filter(([, descriptor]) => !descriptor.ui_only || String(descriptor.type).toLowerCase() === "preset").map(([name, descriptor]) => { const policy = tool.parameters?.[name] || {}; return `<div class="tool-parameter-row"><strong title="${escape(policy.description || descriptor.description || descriptor.label || name)}">${escape(name)}</strong><span>${policy.exposed === false ? "未暴露" : "已暴露"}</span><button class="quiet-button" data-edit-tool-parameter="${escape(name)}" type="button">编辑</button></div>`; }).join(""); return `<h3>${escape(model.name || model.id)}</h3>${modelToggle("tool_enabled", "允许 LLM 调用此模型", tool.enabled !== false)}${modelTextAreaField("tool_selection_description", "什么时候使用", tool.selection_description || "")}${modelSelectField("tool_prompt_profile", "提示词类型", tool.prompt_profile || "natural_language", ["natural_language", "nai_tags", "custom"])}${modelTextAreaField("tool_prompt_instructions", "提示词编写要求", tool.prompt_instructions || "")}${refLimit}<div class="tool-parameter-list"><span class="field-hint">LLM 可用参数</span>${rows || '<span class="field-hint">当前模型没有可暴露参数。</span>'}</div>`; }
  function bindModelConfiguration(provider, model) {
    els.modelForm.querySelectorAll("[data-model-field]").forEach((input) => { input.addEventListener("input", () => updateModelField(input)); input.addEventListener("change", () => updateModelField(input)); });
    els.modelForm.querySelectorAll("[data-schema-default]").forEach((input) => input.addEventListener("change", () => { const descriptor = model.parameters[input.dataset.schemaDefault]; descriptor.default = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value; const raw = $("modelParametersSchema"); if (raw) raw.value = JSON.stringify(model.parameters, null, 2); }));
    els.modelForm.querySelectorAll("[data-tool-field]").forEach((input) => input.addEventListener("input", () => { model.tool[input.dataset.toolField] = input.type === "number" ? Number(input.value) : input.value; }));
    els.modelForm.querySelectorAll("[data-edit-tool-parameter]").forEach((button) => button.addEventListener("click", () => openToolParameterDialog(button.dataset.editToolParameter)));
    $("removeModelButton")?.addEventListener("click", async () => { if (!await confirmAction("删除此模型？历史记录不会删除。")) return; provider.models = provider.models.filter((item) => item.id !== model.id); state.selectedSettingsModelId = provider.models[0]?.id || ""; renderModelEditor(); showNotice("已从设置草稿中删除模型，保存全部设置后生效。", "success"); });
    $("testModelButton")?.addEventListener("click", () => void testModel(provider, model));
  }
  function modelField(key, label, value, type = "text") { return `<div class="field"><label>${label}</label><input data-model-field="${key}" type="${type}" value="${escape(value)}" /></div>`; }
  function modelTextAreaField(key, label, value) { return `<div class="field field-wide"><label>${label}</label><textarea data-model-field="${key}" rows="4">${escape(value)}</textarea></div>`; }
  function modelSelectField(key, label, value, choices, editable = false) { const values = choices.includes(value) ? choices : [value, ...choices]; return `<div class="field"><label>${label}</label><select data-model-field="${key}">${values.map((item) => `<option value="${escape(item)}" ${item === value ? "selected" : ""}>${escape(item)}</option>`).join("")}${editable ? '<option value="__manual__">手动输入…</option>' : ""}</select></div>`; }
  function modelToggle(key, label, value, disabled = false) { return `<div class="toggle-row"><label>${label}</label><label class="toggle-control"><input data-model-field="${key}" type="checkbox" ${value ? "checked" : ""}${disabled ? " disabled" : ""} /><span aria-hidden="true"></span></label></div>`; }
  function updateModelField(input) { const model = currentSettingsModel(); if (!model) return; const key = input.dataset.modelField; if (key === "parameters") { try { model.parameters = input.value.trim() ? JSON.parse(input.value) : {}; input.setCustomValidity(""); } catch { input.setCustomValidity("参数 schema 必须是合法 JSON"); } return; } if (key.startsWith("tool_")) { const toolKey = key.slice(5); model.tool[toolKey] = input.type === "checkbox" ? input.checked : input.value; return; } if (key === "id" && input.value === "__manual__") { const manual = window.prompt("输入模型 ID", model.id); if (!manual?.trim()) { input.value = model.id; return; } input.value = manual.trim(); } model[key] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value; if (key === "supports_img2img") { if (!model[key]) model.tool.max_reference_images = 0; renderModelEditor(); } else if (key === "max_reference_images") { model.max_reference_images = Math.max(0, Math.min(8, Number(input.value) || 0)); model.capability_source = "manual"; if (model.max_reference_images > 0 && Number(model.tool.max_reference_images || 0) > model.max_reference_images) model.tool.max_reference_images = model.max_reference_images; } else if (key === "supports_text2img" || key === "supports_negative_prompt") { renderModelEditor(); } else if (key === "id") { state.selectedSettingsModelId = input.value; const activeRow = els.settingsModelList.querySelector(".provider-row.is-active"); if (activeRow) { activeRow.dataset.settingsModel = input.value; if (!model.name) activeRow.querySelector("strong").textContent = input.value; } } }
  function defaultToolParameterDescription(name, descriptor) {
    const base = descriptor.description || descriptor.label || name;
    const numeric = ["number", "int", "integer", "float"].includes(String(descriptor.type || "").toLowerCase());
    if (!numeric) return base;
    const hasMin = descriptor.min !== undefined && descriptor.min !== null; const hasMax = descriptor.max !== undefined && descriptor.max !== null;
    if (!hasMin && !hasMax) return base;
    const separator = /[。！？.!?]$/.test(base) ? "" : "。";
    const range = hasMin && hasMax ? `取值范围：[${descriptor.min}, ${descriptor.max}]。` : hasMin ? `取值范围：不小于 ${descriptor.min}。` : `取值范围：不大于 ${descriptor.max}。`;
    return `${base}${separator}${range}`;
  }
  function ensureToolConfig(model, provider) {
    const nai = provider.kind === "nai_direct";
    const defaults = { enabled: true, selection_description: nai ? "仅在用户明确要求 NAI 或 NovelAI 风格标签生图时使用。" : "适合一般自然语言生图需求。", prompt_profile: nai ? "nai_tags" : "natural_language", prompt_instructions: nai ? "使用英文逗号分隔标签。必须完整描述主体数量、全身或半身范围、姿态、镜头距离、视角、背景、光照和画面边界，避免残图；不得改变用户明确指定的主体、数量、动作和服装。" : "使用清晰、完整的自然语言描述用户要求。", max_reference_images: model.supports_img2img ? Number(model.max_reference_images || 0) : 0, parameters: {} };
    model.tool = { ...defaults, ...(model.tool || {}) }; model.tool.parameters = model.tool.parameters || {};
    Object.entries(model.parameters || {}).forEach(([name, descriptor]) => {
      if (descriptor.ui_only && String(descriptor.type).toLowerCase() !== "preset") return;
      const current = model.tool.parameters[name] || {}; const legacyDescription = descriptor.description || descriptor.label || name;
      const description = !current.description || current.description === legacyDescription ? defaultToolParameterDescription(name, descriptor) : current.description;
      model.tool.parameters[name] = { exposed: true, ...current, description };
    });
  }
  function toolDefaultChoices(descriptor) { if (!Array.isArray(descriptor?.choices)) return []; return descriptor.choices.flatMap((choice) => { if (choice && typeof choice === "object") { if (!Object.prototype.hasOwnProperty.call(choice, "value")) return []; return [{ value: choice.value, label: choice.label ?? choice.value }]; } return [{ value: choice, label: choice }]; }); }
  function sameToolDefault(left, right) { return JSON.stringify(left) === JSON.stringify(right); }
  function toolDefaultLabel(value) { if (value === undefined) return "未设置"; if (value === "") return "空字符串"; if (value && typeof value === "object") return JSON.stringify(value); return String(value); }
  function openToolParameterDialog(name) {
    const model = currentSettingsModel(); if (!model) return;
    ensureToolConfig(model, currentSettingsProvider());
    const descriptor = model.parameters[name] || {}; const policy = model.tool.parameters[name] || {};
    const choices = toolDefaultChoices(descriptor); const usesChoice = choices.length > 0;
    state.editingToolParameter = name; state.editingToolDefaultChoices = choices;
    $("parameterDialogTitle").textContent = `编辑工具参数：${name}`;
    els.toolParameterExposed.checked = policy.exposed !== false; els.toolParameterDescription.value = policy.description || "";
    els.toolParameterDefault.classList.toggle("is-hidden", usesChoice); els.toolParameterDefaultChoice.classList.toggle("is-hidden", !usesChoice);
    $("toolParameterDefaultLabel").htmlFor = usesChoice ? "toolParameterDefaultChoice" : "toolParameterDefault";
    if (usesChoice) {
      els.toolParameterDefaultChoice.innerHTML = `<option value="${MODEL_DEFAULT_CHOICE}">模型默认值</option>${choices.map((choice, index) => `<option value="${index}">${escape(choice.label)}</option>`).join("")}`;
      const selectedIndex = Object.prototype.hasOwnProperty.call(policy, "default_override") ? choices.findIndex((choice) => sameToolDefault(choice.value, policy.default_override)) : -1;
      els.toolParameterDefaultChoice.value = selectedIndex >= 0 ? String(selectedIndex) : MODEL_DEFAULT_CHOICE;
      els.toolParameterDefaultHint.textContent = `模型配置当前默认值：${toolDefaultLabel(descriptor.default)}`;
      els.toolParameterDefault.value = "";
    } else {
      els.toolParameterDefault.value = policy.default_override ?? ""; els.toolParameterDefaultChoice.innerHTML = "";
      els.toolParameterDefaultHint.textContent = "留空时使用模型配置中的默认值";
    }
    const choiceDescriptions = policy.choice_descriptions;
    els.toolParameterChoices.value = choiceDescriptions && typeof choiceDescriptions === "object" && !Array.isArray(choiceDescriptions) && Object.keys(choiceDescriptions).length ? JSON.stringify(choiceDescriptions, null, 2) : "";
    els.parameterDialog.classList.remove("is-hidden"); els.scrim.classList.remove("is-hidden");
  }
  function closeToolParameterDialog() { state.editingToolParameter = ""; state.editingToolDefaultChoices = []; els.parameterDialog.classList.add("is-hidden"); if (!els.detailDrawer.classList.contains("is-open")) els.scrim.classList.add("is-hidden"); }
  function applyToolParameterDialog() {
    const model = currentSettingsModel(); const name = state.editingToolParameter; if (!model || !name) return;
    let choiceDescriptions = {}; try { choiceDescriptions = els.toolParameterChoices.value.trim() ? JSON.parse(els.toolParameterChoices.value) : {}; } catch { showNotice("选项说明必须是合法 JSON。", "error"); return; }
    if (!choiceDescriptions || typeof choiceDescriptions !== "object" || Array.isArray(choiceDescriptions)) { showNotice("选项说明必须是 JSON 对象。", "error"); return; }
    const descriptor = model.parameters[name] || {}; const policy = { exposed: els.toolParameterExposed.checked, description: els.toolParameterDescription.value };
    if (Object.keys(choiceDescriptions).length) policy.choice_descriptions = choiceDescriptions;
    if (state.editingToolDefaultChoices.length) {
      if (els.toolParameterDefaultChoice.value !== MODEL_DEFAULT_CHOICE) {
        const selected = state.editingToolDefaultChoices[Number(els.toolParameterDefaultChoice.value)];
        if (selected) policy.default_override = selected.value;
      }
    } else if (els.toolParameterDefault.value !== "") {
      policy.default_override = ["number", "int", "integer", "float"].includes(String(descriptor.type).toLowerCase()) ? Number(els.toolParameterDefault.value) : els.toolParameterDefault.value;
    }
    model.tool.parameters[name] = policy; closeToolParameterDialog(); renderModelEditor();
  }
  async function testModel(provider, model) {
    const button = $("testModelButton");
    if (button) { button.disabled = true; button.textContent = "测试中…"; }
    setError(els.settingsError, `正在测试模型 ${model.name || model.id}…`);
    try {
      const payload = await apiPost("model/test", { provider, model_id: model.id });
      setError(els.settingsError, ""); showNotice(`模型测试成功，返回 ${payload.image_count} 张图片。`, "success");
    } catch (error) {
      const message = errorMessage(error, "模型测试失败"); setError(els.settingsError, message); showNotice(message, "error");
    } finally { if (button) { button.disabled = !model.supports_text2img; button.textContent = "测试模型"; } }
  }
  async function addProvider() {
    if (!state.settings && !await loadSettings()) return;
    const id = `provider_${Date.now().toString(36)}`;
    state.settings.webui.providers.push({ id, name: "新服务商", enabled: true, kind: "openai_images", ...providerDefaults("openai_images"), api_key: "", custom_headers: "", timeout_seconds: 180, discovered_models: [], models: [] });
    state.selectedSettingsProviderId = id; state.selectedSettingsModelId = ""; renderSettingsProviders(); showNotice("已新增生图服务商，请填写连接配置并添加模型。", "success");
  }
  async function addModel() {
    if (!state.settings && !await loadSettings()) return;
    const provider = currentSettingsProvider();
    if (!provider) { showNotice("请先选择一个服务商，再新增模型。", "error"); return; }
    provider.models = Array.isArray(provider.models) ? provider.models : [];
    const requestedId = els.newModelChoice.value.trim();
    if (!requestedId) { showNotice("请选择或输入模型 ID。", "error"); els.newModelChoice.focus(); return; }
    if (provider.models.some((item) => item.id === requestedId)) { showNotice("该服务商中已经存在相同模型 ID。", "error"); return; }
    const discovered = (provider.discovered_models || []).find((item) => item.id === requestedId);
    const naiChoice = provider.kind === "nai_direct" ? NAI_MODELS.find((item) => item.id === requestedId) : null;
    const chosen = discovered || (naiChoice ? { ...naiChoice, supports_text2img: true, supports_img2img: false, supports_negative_prompt: true, max_reference_images: 0, capability_source: "builtin" } : null);
    const capabilityKnown = chosen && chosen.capability_source !== "unknown"; const maxRefs = capabilityKnown ? Number(chosen.max_reference_images || 0) : 0;
    provider.models.push({ id: requestedId, name: chosen?.name || requestedId, supports_text2img: chosen ? !!chosen.supports_text2img : true, supports_img2img: capabilityKnown ? !!chosen.supports_img2img : false, supports_negative_prompt: chosen ? !!chosen.supports_negative_prompt : provider.kind === "nai_direct", negative_prompt_default: provider.kind === "nai_direct" ? NAI_DEFAULT_NEGATIVE : "", max_reference_images: maxRefs, capability_source: chosen?.capability_source || "manual", parameters: modelPreset(provider.kind), tool: { enabled: true, max_reference_images: maxRefs } });
    state.selectedSettingsModelId = requestedId; renderModelEditor(); showNotice("已新增模型，请填写能力和参数 schema。", "success");
  }
  async function saveSettings() {
    if (!state.settings && !await loadSettings()) return;
    setError(els.settingsError, "正在保存设置…"); els.saveSettingsButton.disabled = true; els.saveSettingsButton.textContent = "保存中…";
    const webui = state.settings.webui; webui.history = { enabled: els.historyEnabled.checked, retain_reference_images: els.retainReferences.checked, record_invocation_identity: els.recordInvocationIdentity.checked, max_records: Number(els.historyRecords.value), max_megabytes: Number(els.historyMegabytes.value) };
    webui.generation_defaults = { page: { text2img_model_ref: els.settingPageDefaultTextModel.value, img2img_model_ref: els.settingPageDefaultImageModel.value }, tool: { text2img_model_ref: els.settingToolDefaultTextModel.value, img2img_model_ref: els.settingToolDefaultImageModel.value } };
    try {
      await apiPost("settings/save", { settings_revision: webui.revision ?? webui.ui.settings_revision, base: { enable_llm_tool: els.settingTool.checked }, studio: webui });
      await bootstrap(); await loadSettings(); setError(els.settingsError, ""); showNotice("设置已保存并生效。", "success");
    } catch (error) {
      const message = errorMessage(error, "设置保存失败"); setError(els.settingsError, message); showNotice(message, "error");
    } finally { els.saveSettingsButton.disabled = false; els.saveSettingsButton.textContent = "保存全部设置"; }
  }

  function dataUrlToFile(dataUrl, name) { const [head, encoded] = dataUrl.split(",", 2); const type = (head.match(/data:([^;]+)/) || [])[1] || "image/png"; const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0)); return new File([bytes], name, { type }); }
  async function exportSelected() { try { const result = await apiPost("gallery/export", { ids: Array.from(state.selectedIds) }); const client = await bridge(); await client.download(result.download_endpoint, {}, result.filename); showNotice("导出文件已开始下载。", "success"); } catch (error) { showNotice(errorMessage(error, "画廊导出失败"), "error"); } }
  async function deleteSelected() { if (!await confirmAction(`永久删除 ${state.selectedIds.size} 条生成记录及其结果图？`)) return; try { await apiPost("gallery/delete", { ids: Array.from(state.selectedIds) }); await loadGallery(); showNotice("所选生成记录已删除。", "success"); } catch (error) { showNotice(errorMessage(error, "生成记录删除失败"), "error"); } }
  async function useDataUrlAsReference(dataUrl, name) { try { const client = await bridge(); const uploaded = await client.upload("studio/reference/upload", dataUrlToFile(dataUrl, name)); state.references = [uploaded]; state.mode = "img2img"; document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === "img2img")); renderModelChoices(); renderReferences(); closeDetail(); switchView("generate"); setError(els.generationError, "已将当前成图作为新的图生图参考图。它不会被当作历史原始参考图。"); } catch (error) { setError(els.generationError, errorMessage(error, "添加参考图失败")); } }
  function confirmAction(message) { return new Promise((resolve) => { const dialog = $("confirmDialog"); const cancel = $("confirmCancel"); const accept = $("confirmAccept"); $("confirmMessage").textContent = message; dialog.classList.remove("is-hidden"); els.scrim.classList.remove("is-hidden"); accept.focus(); const onKeydown = (event) => { if (event.key === "Escape") finish(false); }; const finish = (value) => { dialog.classList.add("is-hidden"); if (!els.detailDrawer.classList.contains("is-open")) els.scrim.classList.add("is-hidden"); cancel.removeEventListener("click", onCancel); accept.removeEventListener("click", onAccept); document.removeEventListener("keydown", onKeydown); activeConfirmation = null; resolve(value); }; const onCancel = () => finish(false); const onAccept = () => finish(true); activeConfirmation = finish; cancel.addEventListener("click", onCancel); accept.addEventListener("click", onAccept); document.addEventListener("keydown", onKeydown); }); }

  function bindEvents() {
    if (eventsBound) return;
    eventsBound = true;
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
    document.querySelectorAll(".segment").forEach((button) => button.addEventListener("click", () => { state.mode = button.dataset.mode; state.selectedModelRef = state.defaultModelRefs[state.mode] || ""; state.parameterValues = {}; document.querySelectorAll(".segment").forEach((item) => item.classList.toggle("is-active", item === button)); renderModelChoices(); }));
    document.querySelectorAll("[data-default-scope]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-default-scope]").forEach((item) => item.classList.toggle("is-active", item === button)); document.querySelectorAll("[data-default-panel]").forEach((panel) => panel.classList.toggle("is-hidden", panel.dataset.defaultPanel !== button.dataset.defaultScope)); }));
    els.modelChoice.addEventListener("change", () => { state.selectedModelRef = els.modelChoice.value; state.parameterValues = {}; els.negativePrompt.value = selectedModel()?.negative_prompt_default || ""; renderModelWorkspace(); });
    els.resetNegativePromptButton.addEventListener("click", () => { els.negativePrompt.value = selectedModel()?.negative_prompt_default || ""; els.negativePrompt.focus(); });
    els.referenceUpload.addEventListener("change", async () => { try { await uploadReferences(els.referenceUpload.files); } catch (error) { setError(els.generationError, errorMessage(error, "上传参考图失败")); } finally { els.referenceUpload.value = ""; } });
    els.generationForm.addEventListener("submit", generate); $("galleryRefresh").addEventListener("click", () => void loadGallery()); els.gallerySearch.addEventListener("change", () => void loadGallery()); els.galleryProvider.addEventListener("change", () => void loadGallery()); els.galleryMode.addEventListener("change", () => void loadGallery()); els.gallerySource.addEventListener("change", () => void loadGallery());
    $("cancelSelectionButton").addEventListener("click", clearGallerySelection); $("selectAllButton").addEventListener("click", () => { state.galleryItems.forEach((item) => state.selectedIds.add(item.id)); els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => { input.checked = true; }); updateSelection(); }); $("exportButton").addEventListener("click", () => void exportSelected()); $("deleteButton").addEventListener("click", () => void deleteSelected());
    $("closeDrawer").addEventListener("click", closeDetail); $("closeImagePreview").addEventListener("click", closeImagePreview); els.imagePreview.querySelector("[data-close-image-preview]").addEventListener("click", closeImagePreview); els.previewImage.addEventListener("click", closeImagePreview); els.scrim.addEventListener("click", () => { if (!els.parameterDialog.classList.contains("is-hidden")) return; if (activeConfirmation) activeConfirmation(false); else closeDetail(); }); $("parameterDialogCancel").addEventListener("click", closeToolParameterDialog); $("parameterDialogApply").addEventListener("click", applyToolParameterDialog); els.addProviderButton.addEventListener("click", () => void addProvider()); els.addModelButton.addEventListener("click", () => void addModel()); els.saveSettingsButton.addEventListener("click", () => void saveSettings());
    document.addEventListener("keydown", (event) => { if (event.key !== "Escape") return; if (!els.imagePreview.classList.contains("is-hidden")) closeImagePreview(); else if (els.detailDrawer.classList.contains("is-open") && !activeConfirmation) closeDetail(); });
  }

  async function start() {
    bindEvents();
    try { await bootstrap(); }
    catch (error) { const message = errorMessage(error, "页面初始化失败"); els.runtimeStatus.textContent = "页面初始化失败"; showNotice(message, "error"); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => void start(), { once: true });
  else void start();
})();
