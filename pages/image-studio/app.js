(function () {
  "use strict";

  window.__imageStudioAppLoaded = true;

  const state = {
    view: "generate", mode: "text2img", providers: [], models: [], selectedProviderId: "", selectedModelRef: "", defaultModelRefs: { text2img: "", img2img: "" }, parameterValues: {}, parameterCarry: {}, negativePromptCarry: "", hasNegativePromptCarry: false, references: [],
    resultImages: [], galleryItems: [], galleryPage: 0, galleryLimit: 24, galleryTotal: 0, selectedIds: new Set(), settings: null, selectedSettingsProviderId: "", selectedSettingsModelId: "", modelEditorTab: "model", editingToolParameter: "", editingToolDefaultChoices: [], detailId: "", detailData: null, detailFallbackThumbnail: "", detailImageIndex: 0, detailRequestedImageIndex: 0, detailAssetsLoaded: false, detailNavigating: false, imagePreviewItems: [], imagePreviewIndex: 0, imagePreviewTitle: "图片预览", imagePreviewDownloadFilename: "", imagePreviewContext: null, imagePreviewNavigating: false, imagePreviewSwipeAt: 0,
  };
  let activeConfirmation = null;
  let settingsLoadPromise = null;
  let settingsBaseline = "";
  let settingsSaving = false;
  let storageRetention = null;
  let referencesUploading = false;
  let eventsBound = false;
  const providerQuotas = new Map();
  const PROVIDER_QUOTA_TTL = 30_000;
  let providerQuotaTimer = 0;
  let galleryRequestRevision = 0;
  let detailRequestRevision = 0;
  let detailFilmstripScrollFrame = 0;
  let detailBackdropSource = "";
  let detailImagePaintRevision = 0;
  let detailNavigationSession = null;
  let detailAssetsTimer = 0;
  // Only reusable encoded content lives here; displayed DOM/drafts own their
  // references, so LRU eviction never replaces an image that is on screen.
  const imageMedia = new Map();
  const imageMediaLoads = new Map();
  const IMAGE_MEDIA_BYTES = 32 * 1024 * 1024;
  let imageMediaBytes = 0;
  const browseSequences = new Map();
  const browseSequenceLoads = new Map();
  const BROWSE_SEQUENCE_TTL = 30_000;
  let galleryDataRevision = "";
  let browseEpoch = 0;
  const decodedDisplayImages = new Map();
  let mobileImageViewer = null;
  let mobileImageSequence = [];
  let mobileImageDataSource = [];
  let mobileImageLoads = new Map();
  let mobileViewerSession = null;
  let mobileViewerOpenRevision = 0;
  let mobileDetailSyncRevision = 0;
  let mobileViewerOpening = false;
  let suppressMobileDetailSync = false;
  const MODEL_DEFAULT_CHOICE = "__model_default__";
  const EMPTY_MOBILE_IMAGE = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=";
  const $ = (id) => document.getElementById(id);
  const els = {
    pageTitle: $("pageTitle"), pageSubtitle: $("pageSubtitle"), runtimeStatus: $("runtimeStatus"), providerStatus: $("providerStatus"), providerStatusName: $("providerStatusName"), providerQuota: $("providerQuota"),
    modelChoice: $("modelChoice"), modelProvider: $("modelProvider"), workspaceEmpty: $("workspaceEmpty"), generatorWorkspace: $("generatorWorkspace"), modelParameters: $("modelParameters"), referenceField: $("referenceField"), referenceUpload: $("referenceUpload"), referenceStrip: $("referenceStrip"),
    generationForm: $("generationForm"), prompt: $("prompt"), negativePromptField: $("negativePromptField"), negativePrompt: $("negativePrompt"), negativePromptHint: $("negativePromptHint"), resetNegativePromptButton: $("resetNegativePromptButton"), advancedParameters: $("advancedParameters"), parameters: $("parameters"), generationError: $("generationError"), generateButton: $("generateButton"), resultEmpty: $("resultEmpty"), resultGrid: $("resultGrid"), resultMeta: $("resultMeta"),
    galleryGrid: $("galleryGrid"), galleryEmpty: $("galleryEmpty"), galleryPagination: $("galleryPagination"), galleryPrev: $("galleryPrev"), galleryNext: $("galleryNext"), galleryPageLabel: $("galleryPageLabel"), gallerySearch: $("gallerySearch"), galleryProvider: $("galleryProvider"), galleryMode: $("galleryMode"), gallerySource: $("gallerySource"), selectionBar: $("selectionBar"), selectionCount: $("selectionCount"),
    detailDrawer: $("detailDrawer"), drawerBody: $("drawerBody"), detailDate: $("detailDate"), scrim: $("scrim"), imagePreview: $("imagePreview"), imagePreviewBody: $("imagePreviewBody"), imagePreviewPrev: $("imagePreviewPrev"), imagePreviewNext: $("imagePreviewNext"), imagePreviewDots: $("imagePreviewDots"), previewImage: $("previewImage"), imagePreviewTitle: $("imagePreviewTitle"), downloadImageButton: $("downloadImageButton"),
    settingTool: $("settingTool"), agentImageReturnMode: $("agentImageReturnMode"), agentPreviewMaxEdge: $("agentPreviewMaxEdge"), agentPreviewQuality: $("agentPreviewQuality"), agentAssetRetentionHours: $("agentAssetRetentionHours"), storageHealthStatus: $("storageHealthStatus"), storageHealthCheckedAt: $("storageHealthCheckedAt"), storageHealthDuration: $("storageHealthDuration"), storageHealthAssets: $("storageHealthAssets"), storageHealthLeases: $("storageHealthLeases"), storageHealthGenerations: $("storageHealthGenerations"), storageHealthSize: $("storageHealthSize"), storageHealthErrors: $("storageHealthErrors"), runMaintenanceButton: $("runMaintenanceButton"), runDeepMaintenanceButton: $("runDeepMaintenanceButton"), settingPageDefaultTextModel: $("settingPageDefaultTextModel"), settingPageDefaultImageModel: $("settingPageDefaultImageModel"), settingToolDefaultTextModel: $("settingToolDefaultTextModel"), settingToolDefaultImageModel: $("settingToolDefaultImageModel"), historyEnabled: $("historyEnabled"), retainReferences: $("retainReferences"), recordInvocationIdentity: $("recordInvocationIdentity"), historyRecords: $("historyRecords"), historyMegabytes: $("historyMegabytes"), settingsProviderList: $("settingsProviderList"), providerForm: $("providerForm"), settingsModelList: $("settingsModelList"), modelForm: $("modelForm"), settingsError: $("settingsError"), addProviderButton: $("addProviderButton"), addModelButton: $("addModelButton"), newModelChoice: $("newModelChoice"), newModelChoices: $("newModelChoices"), saveSettingsButton: $("saveSettingsButton"), parameterDialog: $("parameterDialog"), toolParameterExposed: $("toolParameterExposed"), toolParameterDescription: $("toolParameterDescription"), toolParameterDefault: $("toolParameterDefault"), toolParameterDefaultChoice: $("toolParameterDefaultChoice"), toolParameterDefaultHint: $("toolParameterDefaultHint"), toolParameterChoices: $("toolParameterChoices"),
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
  async function apiPost(path, body) {
    const result = await (await bridge()).apiPost(path, body);
    if (/^(?:studio\/generate|settings\/save|storage\/maintenance|gallery\/(?:delete|images\/delete|reference\/delete|favorite|import-edit\/[^/]+)|imports\/group\/[^/]+\/commit)$/.test(path)) invalidateBrowseCache();
    return result;
  }

  function imageMediaKey(image, detail) {
    const identity = image?.sha256 || image?.image_id || image?.imageId || image?.id;
    if (!identity) return "";
    return `${identity}:${detail}:${detail === "preview" ? image.thumbnail_revision || "legacy" : "original"}`;
  }

  function getImageMedia(image, detail) {
    const key = imageMediaKey(image, detail), value = imageMedia.get(key);
    if (!value) return "";
    imageMedia.delete(key); imageMedia.set(key, value);
    return value;
  }

  function cacheImageMedia(image, detail, source) {
    const key = imageMediaKey(image, detail);
    if (!key || !source) return source || "";
    const previous = imageMedia.get(key);
    if (previous) { imageMediaBytes -= previous.length * 2; imageMedia.delete(key); }
    // Count UTF-16 conservatively, including base64 overhead. Oversized assets
    // may be displayed but must not displace the whole reusable preview cache.
    if (source.length * 2 > IMAGE_MEDIA_BYTES / 2) return source;
    imageMedia.set(key, source); imageMediaBytes += source.length * 2;
    while (imageMediaBytes > IMAGE_MEDIA_BYTES || imageMedia.size > 256) {
      const oldest = imageMedia.keys().next().value;
      imageMediaBytes -= imageMedia.get(oldest).length * 2; imageMedia.delete(oldest);
    }
    return source;
  }

  function discardImageMediaSource(source) {
    for (const [key, value] of imageMedia) if (value === source) {
      imageMediaBytes -= value.length * 2; imageMedia.delete(key);
    }
    const groups = new Set([state.detailData, ...(detailNavigationSession?.summaries.values() || [])]);
    for (const group of groups) for (const image of group?.images || []) {
      if (image.data_url === source) { delete image.data_url; image._originalLoaded = false; }
      if (image.thumbnail_data_url === source) delete image.thumbnail_data_url;
    }
    for (const item of mobileViewerSession?.items || []) {
      if (item.originalSrc === source) item.originalSrc = "";
      if (item.previewSrc === source) item.previewSrc = "";
    }
  }

  async function loadImageMedia(image, detail) {
    image = { ...image };
    const cached = getImageMedia(image, detail);
    if (cached) return cached;
    const key = imageMediaKey(image, detail);
    const id = image?.image_id || image?.imageId || image?.id;
    if (!key || !id) throw new Error("缺少图片标识，无法读取图片。");
    if (!imageMediaLoads.has(key)) {
      const promise = apiGet(`gallery/image/${id}`, { detail }).then(payload => {
        if (!payload?.data_url) throw new Error("图片接口没有返回可用内容。");
        // Store under the requested revision only: a late request must never
        // overwrite a newer preview after thumbnail settings were changed.
        return cacheImageMedia(image, detail, payload.data_url);
      }).finally(() => { if (imageMediaLoads.get(key) === promise) imageMediaLoads.delete(key); });
      imageMediaLoads.set(key, promise);
    }
    return imageMediaLoads.get(key);
  }

  function reuseImageMedia(detail) {
    for (const image of detail?.images || []) {
      image.thumbnail_data_url ||= getImageMedia(image, "preview");
      const original = getImageMedia(image, "original");
      if (original) { image.data_url = original; image._originalLoaded = true; }
    }
    return detail;
  }

  function browseFilterKey(filters) {
    return JSON.stringify(Object.keys(filters).sort().map(key => {
      let value = filters[key];
      if (key.endsWith("s") && typeof value === "string" && value.startsWith("[")) {
        try { value = JSON.parse(value).sort(); } catch { /* Keep invalid input for the API to explain. */ }
      }
      return [key, value];
    }));
  }

  function invalidateBrowseCache() {
    browseEpoch++; browseSequences.clear(); browseSequenceLoads.clear();
    if (detailNavigationSession?.active) refreshDetailSequence(detailNavigationSession, false);
  }

  function observeGalleryRevision(revision) {
    const value = String(revision || "");
    if (!value || value === galleryDataRevision) return false;
    const [instance, version] = value.split(":"), [previousInstance, previousVersion] = galleryDataRevision.split(":");
    if (instance === previousInstance && Number(version) < Number(previousVersion)) return false;
    galleryDataRevision = value; invalidateBrowseCache();
    return true;
  }

  function configuredReferenceLimit(value, maximum = 8) { const number = Number(value); return Math.max(1, Math.min(maximum, Number.isFinite(number) ? Math.trunc(number) : 1)); }
  function referenceLimitForModel(model) { return model?.supports_img2img ? configuredReferenceLimit(model.max_reference_images) : 0; }
  function modelsForMode() { return state.models.filter((item) => state.mode === "text2img" ? item.supports_text2img : referenceLimitForModel(item) > 0); }
  function selectedModel() { return state.models.find((item) => item.model_ref === state.selectedModelRef) || null; }
  function selectedProvider() { const model = selectedModel(); return state.providers.find((item) => item.id === (model?.provider_id || state.selectedProviderId)) || null; }
  function text(value) { return value === null || value === undefined ? "" : String(value); }
  function escape(value) { const div = document.createElement("div"); div.textContent = text(value); return div.innerHTML.replaceAll('"', "&quot;").replaceAll("'", "&#39;"); }
  function formatDate(value) { return new Date(Number(value) * 1000).toLocaleString(); }
  function formatBytes(value) { const bytes = Number(value || 0); return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(0, Math.round(bytes / 1024))} KB`; }
  function sourceLabel(value) { return ({ webui: "WebUI", command: "指令", llm_tool: "LLM 工具", import: "导入" })[value] || value || "未知"; }
  function invocationSourceLabel(source) { if (!source || !Object.values(source).some((value) => value)) return "未记录"; return { 场景: source.context_type === "group" ? "群聊" : source.context_type === "private" ? "私聊" : source.context_type, 平台: source.platform_name, 平台实例: source.platform_id, 群ID: source.group_id, 群名称: source.group_name, 用户ID: source.user_id, 用户昵称: source.user_name }; }
  function setError(target, message) { target.textContent = message || ""; }
  function errorMessage(error, fallback) {
    const message = error instanceof Error ? error.message : String(error || "");
    if (!message) return fallback;
    if (/[\u3400-\u9fff]/.test(message)) return message;
    if (/network error|failed to fetch/i.test(message)) return `${fallback}：无法连接 AstrBot 后端`;
    if (/request failed with status code/i.test(message)) return `${fallback}：服务请求失败`;
    if (/plugin bridge endpoint/i.test(message)) return `${fallback}：接口路径不符合 AstrBot 页面桥接要求`;
    if (/plugin bridge/i.test(message)) return `${fallback}：页面通信失败`;
    return `${fallback}：${message}`;
  }
  function showNotice(message, tone = "info") {
    window.__showImageStudioNotice(message, tone);
  }

  function quotaProvider() {
    const provider = selectedProvider();
    return state.view === "generate" && selectedModel() && provider?.kind === "nai_direct" ? provider : null;
  }

  function renderProviderStatus() {
    const model = selectedModel(), provider = selectedProvider(), active = quotaProvider();
    els.providerStatusName.textContent = model && provider ? `${model.name} · ${provider.name}` : "未选择模型";
    els.providerStatusName.title = els.providerStatusName.textContent;
    els.providerStatus.classList.toggle("has-provider-quota", !!active);
    els.providerQuota.hidden = !active;
    if (!active) { els.providerQuota.textContent = ""; els.providerQuota.removeAttribute("title"); return; }
    const quota = providerQuotas.get(active.id);
    const failed = !!quota?.error;
    els.providerQuota.classList.toggle("is-unavailable", failed);
    els.providerQuota.classList.toggle("is-warning", !!quota?.data && (!quota.data.enabled || quota.data.remaining === 0));
    els.providerQuota.textContent = failed ? "额度暂不可用" : quota?.data ? `剩余额度 ${quota.data.remaining.toLocaleString("zh-CN")}${quota.data.enabled ? "" : " · 已停用"}` : "额度查询中…";
    els.providerQuota.title = failed ? quota.error : quota?.data ? `服务商：${active.name}\n更新于 ${formatDate(quota.data.checked_at)}` : `正在查询 ${active.name} 的额度`;
  }

  function refreshProviderQuota() {
    const provider = quotaProvider();
    if (!provider || document.hidden) { renderProviderStatus(); return; }
    const previous = providerQuotas.get(provider.id);
    if (previous && (previous.pending || Date.now() - previous.updatedAt < PROVIDER_QUOTA_TTL)) { renderProviderStatus(); return; }
    const entry = { pending: true, updatedAt: 0, data: null, error: "" };
    providerQuotas.set(provider.id, entry);
    renderProviderStatus();
    void apiGet("studio/provider-quota", { provider_id: provider.id }).then((payload) => {
      if (payload?.provider_id !== provider.id || !Number.isSafeInteger(payload.remaining) || payload.remaining < 0 || typeof payload.enabled !== "boolean" || !Number.isFinite(payload.checked_at)) throw new Error("额度查询返回的数据格式不正确");
      entry.data = payload;
    }).catch((error) => { entry.error = errorMessage(error, "额度查询失败"); }).finally(() => {
      entry.pending = false; entry.updatedAt = Date.now();
      // A forced refresh or settings reload can supersede an earlier query.
      if (providerQuotas.get(provider.id) === entry) renderProviderStatus();
    });
  }

  function syncPageScrollLock() {
    const locked = els.detailDrawer.classList.contains("is-open")
      || !els.imagePreview.classList.contains("is-hidden")
      || !!mobileImageViewer
      || !els.parameterDialog.classList.contains("is-hidden")
      || !$('studioModalRoot').classList.contains("is-hidden")
      || !$('confirmDialog').classList.contains("is-hidden");
    document.documentElement.classList.toggle("modal-open", locked);
    document.body.classList.toggle("modal-open", locked);
  }

  function switchView(view) {
    window.ImageStudioSelect?.close();
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
    document.querySelectorAll(".view").forEach((item) => item.classList.toggle("is-active", item.id === `${view}View`));
    const labels = { generate: ["生图", "选择模式和模型后开始创作"], gallery: ["画廊", "搜索、筛选、复现或导出历史生成记录"], import: ["导入", "图片与生成参数"], settings: ["设置", "管理运行策略、历史、生图服务商和模型"], };
    els.pageTitle.textContent = labels[view][0]; els.pageSubtitle.textContent = labels[view][1];
    refreshProviderQuota();
    library.syncFloatingBars();
    if (view === "gallery") void loadGallery();
    if (view === "settings") { void loadSettings(); void loadStorageHealth(); }
    window.ImageStudioSelect?.refresh();
  }

  function collectModelParameters() {
    const values = {};
    effectiveModelParameters(selectedModel()).forEach(([name, descriptor]) => {
      if (descriptor.webui_visible === false && Object.prototype.hasOwnProperty.call(state.parameterValues, name)) values[name] = state.parameterValues[name];
    });
    els.modelParameters.querySelectorAll("[data-model-parameter]").forEach((input) => {
      if (input.dataset.unsetValue === "true") return;
      const key = input.dataset.modelParameter;
      if (input.dataset.nullValue === "true") values[key] = null;
      else if (input.type === "checkbox") values[key] = input.checked;
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
    let value = Object.prototype.hasOwnProperty.call(state.parameterValues, name) ? state.parameterValues[name] : descriptor.default ?? "";
    const requestKey = escape(descriptor.request_key || name);
    if (type === "preset" && Array.isArray(descriptor.choices)) {
      const options = descriptor.choices.map((choice) => `<option value="${escape(choice.value)}" ${String(choice.value) === String(value) ? "selected" : ""}>${escape(choice.label || choice.value)}</option>`).join("");
      return `<div class="field"><label title="${description}">${label}</label><select data-model-parameter="${escape(name)}" data-parameter-type="preset" data-preset-target="${escape(descriptor.target || "")}" data-ui-only="true">${options}</select></div>`;
    }
    if (type === "select" && Array.isArray(descriptor.choices)) {
      const selected = descriptor.choices.some((choice) => String(typeof choice === "object" ? choice.value : choice) === String(value));
      const options = `${selected ? "" : '<option value="" selected>未设置</option>'}${descriptor.choices.map((choice) => { const option = typeof choice === "object" ? choice : { value: choice, label: choice }; return `<option value="${escape(option.value)}" ${String(option.value) === String(value) ? "selected" : ""}>${escape(option.label)}</option>`; }).join("")}`;
      return `<div class="field"><label title="${description}">${label}</label><select data-model-parameter="${escape(name)}" data-parameter-type="select" data-request-key="${requestKey}">${options}</select></div>`;
    }
    if (type === "boolean" || type === "bool") return `<div class="toggle-row" title="${description}"><label>${label}</label><label class="toggle-control"><input data-model-parameter="${escape(name)}" data-request-key="${requestKey}" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`;
    if (type === "json" || type === "object") return `<div class="field field-wide"><label title="${description}">${label}</label><textarea data-model-parameter="${escape(name)}" data-parameter-type="json" data-request-key="${requestKey}" rows="3" spellcheck="false">${escape(typeof value === "string" ? value : JSON.stringify(value || {}, null, 2))}</textarea></div>`;
    if (type === "textarea") return `<div class="field field-wide"><label title="${description}">${label}</label><textarea data-model-parameter="${escape(name)}" data-parameter-type="text" data-request-key="${requestKey}" rows="4">${escape(value)}</textarea></div>`;
    const inputType = ["number", "int", "integer", "float"].includes(type) ? "number" : "text";
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
    const supportsRefs = state.mode === "img2img" && referenceLimitForModel(model) > 0;
    const supportsNegative = !!model?.supports_negative_prompt;
    els.referenceField.classList.toggle("is-hidden", !supportsRefs);
    els.negativePromptField.classList.toggle("is-hidden", !supportsNegative);
    els.advancedParameters.classList.toggle("is-hidden", model?.provider_kind !== "custom_json");
    els.negativePrompt.disabled = !supportsNegative;
    els.negativePrompt.placeholder = "可选";
    els.negativePromptHint.textContent = supportsNegative ? "当前模型会将此字段作为专用反向提示词发送。" : "";
    refreshProviderQuota();
    renderReferences();
    window.ImageStudioSelect?.refresh($("generateView"));
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
    els.modelParameters.innerHTML = effectiveModelParameters(model).filter(([, descriptor]) => descriptor.webui_visible !== false).map(([name, descriptor]) => renderModelParameter(name, descriptor)).join("") || '<div class="workspace-placeholder">该模型没有额外参数。</div>';
    els.modelParameters.querySelectorAll("[data-model-parameter]").forEach((input) => {
      if (!Object.prototype.hasOwnProperty.call(state.parameterValues, input.dataset.modelParameter)) input.dataset.unsetValue = "true";
      const update = () => {
        delete input.dataset.unsetValue;
        state.parameterValues[input.dataset.modelParameter] = input.type === "checkbox" ? input.checked : input.value;
        if (input.dataset.presetTarget) applyParameterPreset(input.dataset.modelParameter, input.value);
        else syncParameterPresets(input.dataset.modelParameter);
      };
      input.addEventListener("input", update); input.addEventListener("change", update);
    });
    effectiveModelParameters(model).forEach(([, descriptor]) => { if (descriptor.target) syncParameterPresets(descriptor.target); });
    renderGenerationForm();
  }

  function modelParameterInput(name) { return Array.from(els.modelParameters.querySelectorAll("[data-model-parameter]")).find((input) => input.dataset.modelParameter === name) || null; }
  function applyParameterPreset(presetName, choiceValue) {
    const descriptor = selectedModel()?.parameters?.[presetName];
    const choice = descriptor?.choices?.find((item) => String(item.value) === String(choiceValue));
    if (!choice || typeof choice.fill !== "string" || !descriptor.target) return;
    const targetInput = modelParameterInput(descriptor.target);
    if (targetInput) { targetInput.value = choice.fill; delete targetInput.dataset.nullValue; delete targetInput.dataset.unsetValue; }
    state.parameterValues[descriptor.target] = choice.fill;
    syncParameterPresets(descriptor.target);
  }
  function syncParameterPresets(targetName) {
    const model = selectedModel(); if (!model) return;
    const targetInput = modelParameterInput(targetName);
    const targetValue = targetInput ? targetInput.value : state.parameterValues[targetName];
    Object.entries(model.parameters || {}).forEach(([name, descriptor]) => {
      if (String(descriptor.type || "").toLowerCase() !== "preset" || descriptor.target !== targetName) return;
      const presetInput = modelParameterInput(name);
      const matched = (descriptor.choices || []).find((choice) => typeof choice.fill === "string" && choice.fill === targetValue);
      const custom = (descriptor.choices || []).find((choice) => choice.value === "custom");
      state.parameterValues[name] = matched?.value ?? custom?.value ?? "";
      if (presetInput) { presetInput.value = state.parameterValues[name]; window.ImageStudioSelect?.refresh(presetInput); }
    });
  }

  function parameterValuesForModel(model, values, { forReproduction = false } = {}) {
    const source = values && typeof values === "object" ? values : {};
    const result = {}, supplied = new Set();
    effectiveModelParameters(model).forEach(([name, descriptor]) => {
      if (Object.prototype.hasOwnProperty.call(descriptor, "default")) result[name] = descriptor.default;
      if (descriptor.webui_visible === false || forReproduction && descriptor.refill_from_history === false) return;
      if (Object.prototype.hasOwnProperty.call(source, name)) { result[name] = source[name]; supplied.add(name); }
      else if (descriptor.request_key && Object.prototype.hasOwnProperty.call(source, descriptor.request_key)) { result[name] = source[descriptor.request_key]; supplied.add(name); }
    });
    effectiveModelParameters(model).forEach(([name, descriptor]) => {
      if (String(descriptor.type).toLowerCase() !== "preset" || !descriptor.target) return;
      if (!forReproduction && !supplied.has(descriptor.target)) {
        const choice = descriptor.choices?.find((item) => String(item.value) === String(result[name]));
        if (choice && typeof choice.fill === "string") result[descriptor.target] = choice.fill;
      }
      const matched = descriptor.choices?.find((choice) => typeof choice.fill === "string" && choice.fill === result[descriptor.target]);
      const custom = descriptor.choices?.find((choice) => choice.value === "custom");
      result[name] = matched?.value ?? custom?.value ?? "";
    });
    return result;
  }

  function carriedParameterValues() {
    const values = { ...state.parameterCarry, ...state.parameterValues, ...collectModelParameters() };
    effectiveModelParameters(selectedModel()).forEach(([name, descriptor]) => { if (descriptor.webui_visible === false) { delete values[name]; if (descriptor.request_key) delete values[descriptor.request_key]; } });
    Object.entries(selectedModel()?.parameters || {}).forEach(([name, descriptor]) => { if (descriptor?.request_key && Object.prototype.hasOwnProperty.call(values, name)) values[descriptor.request_key] = values[name]; });
    return values;
  }

  function applyGenerationSelection(mode, modelRef) {
    const previousModel = selectedModel(); if (previousModel?.supports_negative_prompt) { state.negativePromptCarry = els.negativePrompt.value; state.hasNegativePromptCarry = true; }
    const carried = carriedParameterValues(); state.parameterCarry = carried; state.mode = mode; state.selectedModelRef = modelRef || "";
    state.parameterValues = parameterValuesForModel(selectedModel(), carried);
    const nextModel = selectedModel(); if (nextModel?.supports_negative_prompt) { els.negativePrompt.value = state.hasNegativePromptCarry ? state.negativePromptCarry : (nextModel.negative_prompt_default || ""); state.negativePromptCarry = els.negativePrompt.value; state.hasNegativePromptCarry = true; }
    document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === state.mode));
    renderModelChoices();
  }

  function renderReferences() {
    const maximum = referenceLimitForModel(selectedModel());
    const disabled = referencesUploading || state.mode !== "img2img" || state.references.length >= maximum;
    $("referenceCount").textContent = `${state.references.length}/${maximum} 张`;
    $("referenceChooseButton").disabled = disabled;
    $("referenceChooseButton").setAttribute("aria-busy", String(referencesUploading));
    els.referenceUpload.disabled = disabled;
    els.referenceStrip.innerHTML = state.references.map((item, index) => `<div class="reference-item"><img src="${item.preview_data_url}" alt="参考图 ${index + 1}" /><button type="button" data-reference-index="${index}" aria-label="移除参考图"><span aria-hidden="true">×</span></button></div>`).join("");
    els.referenceStrip.querySelectorAll("[data-reference-index]").forEach((button) => button.addEventListener("click", () => { state.references.splice(Number(button.dataset.referenceIndex), 1); renderReferences(); }));
  }

  async function bootstrap() {
    const payload = await apiGet("studio/bootstrap");
    providerQuotas.clear();
    state.providers = Array.isArray(payload.providers) ? payload.providers : [];
    state.models = Array.isArray(payload.models) ? payload.models : [];
    state.parameterValues = {}; state.parameterCarry = {}; state.negativePromptCarry = ""; state.hasNegativePromptCarry = false;
    state.defaultModelRefs = { text2img: payload.defaults?.text2img_model_ref || "", img2img: payload.defaults?.img2img_model_ref || "" };
    state.selectedModelRef = state.defaultModelRefs[state.mode] || "";
    state.parameterValues = parameterValuesForModel(selectedModel(), {});
    els.negativePrompt.value = selectedModel()?.negative_prompt_default || "";
    if (selectedModel()?.supports_negative_prompt) { state.negativePromptCarry = els.negativePrompt.value; state.hasNegativePromptCarry = true; }
    els.runtimeStatus.textContent = `已加载 ${state.providers.length} 个生图服务商`;
    renderModelChoices();
  }

  async function uploadReferences(files) {
    if (referencesUploading || state.mode !== "img2img") return;
    const model = selectedModel();
    const maximum = referenceLimitForModel(model);
    const available = Math.max(0, maximum - state.references.length);
    const chosen = Array.from(files).slice(0, available);
    if (!chosen.length) return;
    const targetReferences = state.references;
    const isCurrent = () => state.references === targetReferences && state.selectedModelRef === model.model_ref && state.mode === "img2img" && state.references.length < referenceLimitForModel(selectedModel());
    referencesUploading = true; renderReferences();
    try {
      const client = await bridge();
      for (const file of chosen) {
        if (!isCurrent()) break;
        const uploaded = await client.upload("studio/reference/upload", file);
        if (!isCurrent()) break;
        state.references.push(uploaded); renderReferences();
      }
    } finally {
      referencesUploading = false; renderReferences();
    }
  }

  async function generate(event) {
    event.preventDefault(); setError(els.generationError, "");
    if (referencesUploading) { setError(els.generationError, "请等待参考图上传完成。"); return; }
    let parameters = {};
    if (els.parameters.value.trim()) {
      try { parameters = JSON.parse(els.parameters.value); } catch { setError(els.generationError, "高级参数必须是合法 JSON"); return; }
    }
    const model = selectedModel();
    const provider = selectedProvider();
    if (!model || !provider) { setError(els.generationError, "请先选择支持当前模式的模型"); return; }
    if (state.mode === "img2img" && !state.references.length) { setError(els.generationError, "图生图需要至少一张参考图"); return; }
    const schema = model.parameters || {};
    const mappedParameters = Object.fromEntries(Object.entries(collectModelParameters()).map(([name, value]) => [schema[name]?.request_key || name, value]));
    for (const [name, descriptor] of effectiveModelParameters(model)) {
      const key = descriptor.request_key || name;
      if (descriptor.webui_visible === false && Object.prototype.hasOwnProperty.call(parameters, key)) delete mappedParameters[key];
    }
    const size = mappedParameters.size || "";
    const count = mappedParameters.count ?? mappedParameters.n ?? 1;
    delete mappedParameters.size;
    delete mappedParameters.count;
    delete mappedParameters.n;
    for (const key of MODEL_SCHEDULING_FIELDS) { delete parameters[key]; delete mappedParameters[key]; }
    for (const [name, descriptor] of Object.entries(schema)) if (modelParameterMatches(name, descriptor, MODEL_SCHEDULING_FIELDS)) { delete parameters[name]; delete parameters[descriptor.request_key]; }
    els.generateButton.disabled = true; els.generateButton.textContent = "生成中";
    try {
      const result = await apiPost("studio/generate", { mode: state.mode, provider_id: provider.id, model_ref: model.model_ref, prompt: els.prompt.value, negative_prompt: els.negativePrompt.value, model: model.id, size, count: Number(count), parameters: { ...parameters, ...mappedParameters }, reference_ids: state.references.map((item) => item.id) });
      state.resultImages = result.images || []; state.references = []; renderReferences();
      els.resultEmpty.classList.toggle("is-hidden", state.resultImages.length > 0); els.resultGrid.innerHTML = state.resultImages.map((image, index) => `<div class="result-card"><div class="result-frame"><img class="result-image-backdrop" src="${image.data_url}" alt="" aria-hidden="true" /><img class="result-image" src="${image.data_url}" alt="生成结果" data-result-preview="${index}" /></div><div class="result-card-actions"><button class="quiet-button" data-result-reference="${index}" type="button">用作参考图</button></div></div>`).join("");
      els.resultGrid.querySelectorAll("[data-result-reference]").forEach((button) => button.addEventListener("click", () => void useDataUrlAsReference(state.resultImages[Number(button.dataset.resultReference)].data_url, "generated-reference.png")));
      els.resultGrid.querySelectorAll("[data-result-preview]").forEach((image) => image.addEventListener("click", () => { const index = Number(image.dataset.resultPreview); openImagePreview(image.src, `生成结果-${index + 1}`, state.resultImages[index]?.download_filename, state.resultImages, index); }));
      els.resultMeta.textContent = `${result.provider_name} · ${result.model} · ${(result.elapsed_ms / 1000).toFixed(1)} 秒${result.generation_id ? " · 已保存到画廊" : " · 历史未保留"}`;
      if (result.warning) { setError(els.generationError, result.warning); showNotice(result.warning); }
    } catch (error) { setError(els.generationError, errorMessage(error, "生成失败")); }
    finally {
      els.generateButton.disabled = false; els.generateButton.textContent = "生成图片";
      if (provider.kind === "nai_direct") { providerQuotas.delete(provider.id); refreshProviderQuota(); }
    }
  }

  async function loadGallery(page = state.galleryPage) {
    if (state.view === "gallery") state.galleryLimit = library.galleryPageSize();
    const requestedPage = Math.max(0, Number.isFinite(Number(page)) ? Math.floor(Number(page)) : 0);
    const revision = ++galleryRequestRevision;
    try {
      const payload = await apiGet("gallery/list", { ...galleryFilters(), limit: state.galleryLimit, offset: requestedPage * state.galleryLimit });
      if (revision !== galleryRequestRevision) return false;
      const total = Math.max(0, Number(payload.total || 0));
      const limit = Math.max(1, Number(payload.limit || state.galleryLimit));
      const totalPages = Math.max(1, Math.ceil(total / limit));
      if (total > 0 && requestedPage >= totalPages) { state.galleryPage = totalPages - 1; return await loadGallery(state.galleryPage); }
      state.galleryPage = Math.min(requestedPage, totalPages - 1); state.galleryLimit = limit; state.galleryTotal = total;
      const dataChanged = payload.revision ? observeGalleryRevision(payload.revision) : true;
      if (!payload.revision) invalidateBrowseCache();
      state.galleryItems = payload.items || []; renderGallery(payload);
      if (detailNavigationSession?.active && browseFilterKey(detailNavigationSession.filters) !== browseFilterKey(galleryFilters())) refreshDetailSequence(detailNavigationSession);
      else if (dataChanged && detailNavigationSession?.active) void warmDetailNeighbors(detailNavigationSession);
      return true;
    } catch (error) {
      showNotice(errorMessage(error, "画廊加载失败"), "error");
      return false;
    }
  }

  function galleryFilters() {
    const filters = { query: els.gallerySearch.value, favorite: $("galleryFavorite").value };
    for (const [id, key] of [["galleryProvider", "provider_ids"], ["galleryMode", "modes"], ["gallerySource", "sources"], ["galleryEngine", "generation_engines"]]) {
      const select = $(id);
      const values = Array.from(select.selectedOptions, (option) => option.value);
      if (values.length !== select.options.length) filters[key] = JSON.stringify(values);
    }
    return filters;
  }

  function updateGalleryFilterOptions(select, entries) {
    const previous = Array.from(select.options);
    if (JSON.stringify(previous.map((option) => [option.value, option.label])) === JSON.stringify(entries)) return;
    const all = previous.every((option) => option.selected);
    const selected = new Set(previous.filter((option) => option.selected).map((option) => option.value));
    select.replaceChildren(...entries.map(([value, label]) => new Option(label, value, all, all || selected.has(value))));
    window.ImageStudioSelect?.refresh(select);
  }

  function renderGallery(payload) {
    updateGalleryFilterOptions(els.galleryProvider, [["", "未指定服务商"], ...(payload.filters?.providers || []).map((item) => [item.id, item.name || item.id])]);
    const engineSelect = $("galleryEngine");
    const engines = new Set(Array.from(engineSelect.options, (option) => option.value));
    for (const engine of payload.filters?.generation_engines || []) engines.add(engine === "nai" ? "novelai" : engine);
    updateGalleryFilterOptions(engineSelect, Array.from(engines, (engine) => [engine, library.engineLabel(engine)]));
    els.galleryEmpty.classList.toggle("is-hidden", state.galleryItems.length > 0);
    const filters = galleryFilters();
    els.galleryEmpty.textContent = filters.query || filters.favorite || Object.keys(filters).length > 2 ? "没有符合当前筛选条件的图片。" : "画廊中还没有保留的生成记录。";
    const existing = new Map(Array.from(els.galleryGrid.children, (card) => [card.dataset.galleryId, card]));
    const wanted = new Set(state.galleryItems.map((item) => item.id));
    for (const [id, card] of existing) if (!wanted.has(id)) card.remove();
    // Unchanged records keep their decoded images, focus and hover state across refreshes.
    state.galleryItems.forEach((item, index) => {
      cacheImageMedia(item, "preview", item.thumbnail_data_url);
      const { thumbnail_data_url: thumbnail, ...fields } = item;
      const signature = JSON.stringify(fields);
      let card = existing.get(item.id);
      if (!card || card.gallerySignature !== signature || card.galleryThumbnail !== thumbnail) {
        const template = document.createElement("template");
        template.innerHTML = library.renderGalleryCard(item, index);
        const updated = template.content.firstElementChild;
        const oldImage = card?.querySelector(".gallery-image-wrap > img");
        const newImage = updated.querySelector(".gallery-image-wrap > img");
        if (oldImage && newImage && oldImage.getAttribute("src") === newImage.getAttribute("src")) {
          oldImage.alt = newImage.alt; newImage.replaceWith(oldImage);
        }
        if (card) card.replaceWith(updated);
        card = updated; card.gallerySignature = signature; card.galleryThumbnail = thumbnail;
      }
      card.classList.toggle("is-selected", state.selectedIds.has(item.id));
      card.querySelector("[data-select-id]").checked = state.selectedIds.has(item.id);
      if (els.galleryGrid.children[index] !== card) els.galleryGrid.insertBefore(card, els.galleryGrid.children[index] || null);
    });
    const limit = Math.max(1, Number(payload.limit || state.galleryLimit)); const total = Math.max(0, Number(payload.total || state.galleryTotal)); const totalPages = Math.max(1, Math.ceil(total / limit));
    els.galleryPagination.classList.toggle("is-hidden", totalPages <= 1); els.galleryPageLabel.textContent = `第 ${state.galleryPage + 1} / ${totalPages} 页 · 共 ${total} 条`; els.galleryPrev.disabled = state.galleryPage <= 0; els.galleryNext.disabled = state.galleryPage >= totalPages - 1;
    updateSelection();
    library.galleryRendered(payload);
    window.ImageStudioSelect?.refresh();
  }

  function updateSelection() { els.selectionBar.classList.toggle("is-hidden", state.selectedIds.size === 0); els.selectionCount.textContent = `已选 ${state.selectedIds.size} 项`; els.galleryGrid.querySelectorAll("[data-gallery-id]").forEach((card) => card.classList.toggle("is-selected", state.selectedIds.has(card.dataset.galleryId))); library.selectionChanged(); library.syncFloatingBars(); }
  function clearGallerySelection() { state.selectedIds.clear(); els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => { input.checked = false; }); updateSelection(); }

  function requestParameters(detail) {
    const parameters = detail?.parameters && typeof detail.parameters === "object" ? detail.parameters : {};
    return { mode: detail?.mode || "", model: detail?.model || "", prompt: detail?.original_prompt ?? "", negative_prompt: parameters.negative_prompt ?? "", size: parameters.size ?? "", count: parameters.count ?? 1, parameters: parameters.parameters || {} };
  }

  function detailDisplayImages(detail, fallbackThumbnail = "") {
    const images = Array.isArray(detail.images) ? detail.images : [];
    if (!images.length) return fallbackThumbnail ? [{ data_url: fallbackThumbnail, mime_type: "image/webp", size_bytes: 0 }] : [];
    return images.map((item, index) => ({
      ...item,
      data_url: item?.data_url || item?.thumbnail_data_url || (index === 0 ? fallbackThumbnail : ""),
    }));
  }

  function detailReferenceMarkup(refs) {
    return refs.length ? refs.map((item) => item.available ? item.data_url ? `<article class="detail-reference"><img src="${escape(item.data_url)}" alt="${escape(item.filename)}" data-detail-reference="${item.id}" /><div>${escape(item.filename)}<br>${formatBytes(item.size_bytes)}</div><button class="danger-button" data-reference-delete="${item.id}" type="button">删除参考图</button></article>` : `<article class="detail-reference" data-reference-load="${item.id}"><div>${escape(item.filename)}<br>参考图正在加载…</div></article>` : `<article class="detail-reference"><div>参考图已删除</div></article>`).join("") : "<span>该记录没有保留参考图</span>";
  }

  function watchDetailReferences(detail) {
    const session = detailNavigationSession;
    session?.referenceObserver?.disconnect();
    if (!detail.lightweight || !currentDetailSession(session)) return;
    const currentImage = detail.images?.[state.detailImageIndex];
    // Wait for the parameter block to establish its height before testing
    // reference visibility; the initial loading placeholder is much shorter.
    if (!currentImage?._metadataLoaded && !currentImage?._metadataError) return;
    const load = async (element) => {
      const reference = detail.references.find(item => item.id === element.dataset.referenceLoad);
      if (!reference || reference._loading || reference.data_url) return;
      reference._loading = true;
      try {
        const result = await queueDetailRead(session, `reference:${reference.id}`, () => apiGet(`gallery/reference-image/${reference.id}`), () => state.detailData === detail && element.isConnected);
        if (!result || state.detailData !== detail || !currentDetailSession(session)) return;
        Object.assign(reference, result);
        const currentElement = els.drawerBody.querySelector(`[data-reference-load="${reference.id}"]`);
        if (currentElement) { currentElement.outerHTML = detailReferenceMarkup([reference]); bindDetailReferenceEvents(detail); }
      } catch (error) {
        if (element.isConnected) { element.textContent = errorMessage(error, "参考图加载失败"); const retry = document.createElement("button"); retry.className = "quiet-button"; retry.textContent = "重试"; retry.addEventListener("click", () => void load(element)); element.appendChild(retry); }
      } finally { reference._loading = false; }
    };
    const placeholders = els.drawerBody.querySelectorAll('[data-reference-load]');
    if (window.IntersectionObserver) {
      session.referenceObserver = new IntersectionObserver(entries => { for (const entry of entries) if (entry.isIntersecting) void load(entry.target); }, { root: els.drawerBody, rootMargin: "150px" });
      placeholders.forEach(element => session.referenceObserver.observe(element));
    } else placeholders.forEach(element => void load(element));
  }

  function bindDetailReferenceEvents(detail) {
    els.drawerBody.querySelectorAll("[data-detail-reference]").forEach((image) => { if (image.dataset.referenceBound) return; image.dataset.referenceBound = "1"; image.addEventListener("click", () => openImagePreview(image.src, image.alt, image.alt)); });
    els.drawerBody.querySelectorAll("[data-reference-delete]").forEach((button) => { if (button.dataset.referenceBound) return; button.dataset.referenceBound = "1"; button.addEventListener("click", async () => {
      if (!await confirmAction("删除此参考图？生成结果和参数不会删除。")) return;
      try { await apiPost("gallery/reference/delete", { reference_id: button.dataset.referenceDelete }); showNotice("参考图已删除。", "success"); await openDetail(detail.id, state.detailImageIndex); }
      catch (error) { showNotice(errorMessage(error, "参考图删除失败"), "error"); }
    }); });
  }

  function hasAdjacentGalleryRecord(direction) {
    const sequence = detailNavigationSession?.sequence;
    if (sequence) {
      const position = detailSequencePosition(detailNavigationSession);
      return position >= 0 && sequence.some((item, index) => (direction < 0 ? index < position : index > position) && String(item.generation_id) !== String(state.detailId));
    }
    const index = state.galleryItems.findIndex((item) => String(item.id) === String(state.detailId));
    if (index < 0) return false;
    if (direction < 0) return index > 0 || state.galleryPage > 0;
    return index < state.galleryItems.length - 1 || (state.galleryPage * state.galleryLimit) + index < state.galleryTotal - 1;
  }

  function currentDetailSession(session) {
    return !!session?.active && session === detailNavigationSession && els.detailDrawer.classList.contains("is-open");
  }

  function detailSequencePosition(session) {
    const imageId = state.detailData?.images?.[state.detailImageIndex]?.id;
    if (!imageId || !session?.sequence) return -1;
    return session.sequence.findIndex((item) => String(item.generation_id) === String(state.detailId) && String(item.image_id) === String(imageId));
  }

  function stopDetailNavigation() {
    if (detailNavigationSession) {
      detailNavigationSession.active = false;
      detailNavigationSession.previewObserver?.disconnect();
      detailNavigationSession.referenceObserver?.disconnect();
      detailNavigationSession.queue?.splice(0).forEach((job) => job.resolve(null));
    }
    detailNavigationSession = null;
    window.clearTimeout(detailAssetsTimer); detailAssetsTimer = 0;
  }

  function createDetailNavigation() {
    stopDetailNavigation();
    return detailNavigationSession = { active: true, filters: galleryFilters(), sequence: null, sequenceAt: 0, sequencePromise: null, epoch: 0, summaries: new Map(), loads: new Map(), reads: new Map(), queue: [], reading: 0 };
  }

  function queueDetailRead(session, key, run, wanted = () => true, priority = false) {
    if (!currentDetailSession(session)) return Promise.resolve(null);
    if (session.reads.has(key)) return session.reads.get(key);
    const epoch = session.epoch;
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    const job = { run, resolve, reject, wanted: () => currentDetailSession(session) && session.epoch === epoch && wanted() };
    session.reads.set(key, promise);
    const cleanup = () => { if (session.reads.get(key) === promise) session.reads.delete(key); };
    promise.then(cleanup, cleanup);
    priority ? session.queue.unshift(job) : session.queue.push(job);
    drainDetailReads(session);
    return promise;
  }

  function drainDetailReads(session) {
    while (session.reading < 3 && session.queue.length) {
      const job = session.queue.shift();
      if (!job.wanted()) { job.resolve(null); continue; }
      session.reading++;
      Promise.resolve().then(job.run).then(job.resolve, job.reject).finally(() => { session.reading--; drainDetailReads(session); });
    }
  }

  function isCurrentDetailImage(detail, image, session = detailNavigationSession) {
    return currentDetailSession(session) && state.detailData === detail && detail.images?.[state.detailImageIndex] === image;
  }

  function pruneDetailImages(session) {
    const groups = new Set([state.detailData, ...session.summaries.values()]);
    const metadata = [];
    for (const detail of groups) {
      if (!detail?.lightweight) continue;
      detail.images.forEach((image, index) => {
        const nearby = detail === state.detailData && Math.abs(index - state.detailImageIndex) <= 1;
        if (!nearby) { delete image.data_url; image._originalLoaded = false; }
        if (image._metadataLoaded) metadata.push({ detail, image, nearby });
      });
    }
    metadata.sort((a, b) => Number(b.nearby) - Number(a.nearby) || (b.image._access || 0) - (a.image._access || 0));
    for (const { image } of metadata.slice(8)) { delete image.metadata; delete image.supplemental; image._metadataLoaded = false; }
  }

  async function loadDetailPreview(detail, image, session = detailNavigationSession, wanted = () => true) {
    image.thumbnail_data_url ||= getImageMedia(image, "preview");
    if (image.thumbnail_data_url) return image.thumbnail_data_url;
    const epoch = session?.epoch;
    const source = await queueDetailRead(session, `preview:${image.id}`, () => loadImageMedia(image, "preview"), wanted, isCurrentDetailImage(detail, image, session));
    if (!source || !currentDetailSession(session) || session.epoch !== epoch) return "";
    image.thumbnail_data_url = source;
    updateFilmstripPreview(detail, image);
    return image.thumbnail_data_url;
  }

  async function loadDetailMetadata(detail, image, session = detailNavigationSession) {
    if (image._metadataLoaded) { image._access = Date.now(); return image; }
    const epoch = session?.epoch;
    const payload = await queueDetailRead(session, `metadata:${image.id}`, () => apiGet(`gallery/image-info/${image.id}`, { include_preview: "0" }), () => isCurrentDetailImage(detail, image, session), true);
    if (!payload || !currentDetailSession(session) || session.epoch !== epoch) return null;
    Object.assign(image, payload.image, { data_url: image.data_url || payload.image?.data_url || "", thumbnail_data_url: image.thumbnail_data_url || payload.image?.thumbnail_data_url || "", _metadataLoaded: true, _access: Date.now() });
    Object.assign(detail, payload.detail_fields || {}, { lightweight: true });
    delete image._metadataError;
    updateFilmstripPreview(detail, image);
    pruneDetailImages(session);
    return image;
  }

  async function loadDetailOriginal(detail, image, session = detailNavigationSession) {
    const cached = getImageMedia(image, "original");
    if (cached) { image.data_url = cached; image._originalLoaded = true; }
    if (image._originalLoaded && image.data_url) return image;
    const epoch = session?.epoch;
    const source = await queueDetailRead(session, `original:${image.id}`, () => loadImageMedia(image, "original"), () => isCurrentDetailImage(detail, image, session), true);
    if (!source || !currentDetailSession(session) || session.epoch !== epoch || !isCurrentDetailImage(detail, image, session)) return null;
    image.data_url = source; image._originalLoaded = !!source;
    pruneDetailImages(session);
    return image;
  }

  async function ensureDetailMetadata() {
    const detail = state.detailData, image = detail?.images?.[state.detailImageIndex];
    if (detail?._manifestPending) return null;
    if (!detail?.lightweight || !image) return image;
    return loadDetailMetadata(detail, image);
  }

  async function ensureDetailPreview(image, wanted) {
    const detail = state.detailData;
    if (!detail?.images?.includes(image)) return "";
    return loadDetailPreview(detail, image, detailNavigationSession, wanted);
  }

  function updateFilmstripPreview(detail, image) {
    if (state.detailData !== detail) return;
    const index = detail.images.indexOf(image);
    const mount = els.drawerBody.querySelector(`[data-detail-dot="${index}"] .detail-filmstrip-preview`);
    if (!mount || !image.thumbnail_data_url) return;
    let preview = mount.querySelector("img");
    if (!preview) { preview = document.createElement("img"); preview.alt = ""; preview.draggable = false; preview.decoding = "async"; mount.appendChild(preview); }
    if (preview.getAttribute("src") !== image.thumbnail_data_url) preview.src = image.thumbnail_data_url;
    mount.querySelector(".detail-filmstrip-placeholder").hidden = true;
  }

  function watchDetailPreviews(detail, strip) {
    const session = detailNavigationSession;
    session?.previewObserver?.disconnect();
    if (!detail.lightweight || !strip || !currentDetailSession(session)) return;
    const load = (button) => {
      const image = detail.images[Number(button.dataset.detailDot)];
      const wanted = () => state.detailData === detail && button.isConnected && (() => { const a = button.getBoundingClientRect(), b = strip.getBoundingClientRect(); return a.right >= b.left - 60 && a.left <= b.right + 60; })();
      if (image && !image.thumbnail_data_url) void loadDetailPreview(detail, image, session, wanted).catch(() => {});
    };
    if (window.IntersectionObserver) {
      session.previewObserver = new IntersectionObserver((entries) => { for (const entry of entries) if (entry.isIntersecting) load(entry.target); }, { root: strip, rootMargin: "0px 60px", threshold: 0 });
      strip.querySelectorAll("[data-detail-dot]").forEach(button => session.previewObserver.observe(button));
    } else strip.querySelectorAll("[data-detail-dot]").forEach(button => { if (Math.abs(Number(button.dataset.detailDot) - state.detailImageIndex) <= 1) load(button); });
  }

  function updateDetailNavigationButtons() {
    const count = state.detailData?.images?.length || 0;
    const previous = els.drawerBody.querySelector('[data-detail-nav="-1"]');
    const next = els.drawerBody.querySelector('[data-detail-nav="1"]');
    if (previous) previous.disabled = state.detailImageIndex <= 0 && !hasAdjacentGalleryRecord(-1);
    if (next) next.disabled = state.detailImageIndex >= count - 1 && !hasAdjacentGalleryRecord(1);
  }

  function refreshDetailSequence(session, warm = true) {
    session.epoch++; session.filters = galleryFilters(); session.sequence = null; session.sequenceAt = 0; session.sequencePromise = null;
    session.summaries.clear(); session.loads.clear();
    session.reads.clear();
    if (warm) void warmDetailNeighbors(session);
  }

  async function ensureDetailSequence(session) {
    if (!currentDetailSession(session)) return [];
    if (session.sequence && Date.now() - session.sequenceAt < BROWSE_SEQUENCE_TTL) return session.sequence;
    if (session.sequencePromise) return session.sequencePromise;
    const epoch = session.epoch;
    const cacheEpoch = browseEpoch;
    const key = `${galleryDataRevision}:${browseFilterKey(session.filters)}`;
    const cached = browseSequences.get(key);
    let read;
    if (cached && Date.now() - cached.at < BROWSE_SEQUENCE_TTL) read = Promise.resolve(cached);
    else {
      if (!browseSequenceLoads.has(key)) {
        const request = apiGet("gallery/image-sequence", session.filters).then(payload => {
          const entry = { items: Array.isArray(payload.items) ? payload.items : [], at: Date.now(), revision: payload.revision || galleryDataRevision };
          if (cacheEpoch === browseEpoch && (!payload.revision || !galleryDataRevision || payload.revision === galleryDataRevision)) {
            browseSequences.set(key, entry);
            while (browseSequences.size > 3) browseSequences.delete(browseSequences.keys().next().value);
          }
          return entry;
        }).finally(() => { if (browseSequenceLoads.get(key) === request) browseSequenceLoads.delete(key); });
        browseSequenceLoads.set(key, request);
      }
      read = browseSequenceLoads.get(key);
    }
    const promise = read.then((entry) => {
      if (!currentDetailSession(session) || epoch !== session.epoch) return [];
      session.sequence = entry.items; session.sequenceAt = entry.at; session.sequenceRevision = entry.revision;
      updateDetailNavigationButtons(); return session.sequence;
    }).finally(() => { if (session.sequencePromise === promise) session.sequencePromise = null; });
    session.sequencePromise = promise;
    return promise;
  }

  function detailNeighborCursor(direction, session = detailNavigationSession) {
    const sequence = session?.sequence;
    if (sequence) {
      const position = detailSequencePosition(session);
      return position >= 0 ? sequence[position + direction] || null : null;
    }
    const index = state.detailImageIndex + direction;
    const image = state.detailData?.images?.[index];
    return image ? { generation_id: state.detailId, image_id: image.id, image_index: index } : null;
  }

  function cachedDetailNeighbor(direction) {
    const cursor = detailNeighborCursor(direction);
    if (!cursor) return null;
    const detail = String(cursor.generation_id) === String(state.detailId) ? state.detailData : detailNavigationSession?.summaries.get(String(cursor.generation_id));
    const image = detail?.images?.find((item) => String(item.id) === String(cursor.image_id));
    const src = image?.thumbnail_data_url || getImageMedia(cursor, "preview") || image?.data_url || getImageMedia(cursor, "original");
    return src ? { src, cursor } : null;
  }

  function readDetailSummary(id, session) {
    const cached = session.summaries.get(id);
    if (cached) return Promise.resolve(cached);
    if (!session.loads.has(id)) {
      const epoch = session.epoch;
      const promise = apiGet(`gallery/detail/${id}`, { light: "1" }).then(summary => {
        reuseImageMedia(summary);
        if (currentDetailSession(session) && epoch === session.epoch) {
          session.summaries.set(id, summary);
          while (session.summaries.size > 3) session.summaries.delete(session.summaries.keys().next().value);
        }
        return summary;
      }).finally(() => { if (session.loads.get(id) === promise) session.loads.delete(id); });
      session.loads.set(id, promise);
    }
    return session.loads.get(id);
  }

  function provisionalDetail(cursor, session) {
    const id = String(cursor.generation_id);
    const entries = session.sequence?.filter(item => String(item.generation_id) === id) || [];
    if (!entries.length || !entries.some(item => String(item.image_id) === String(cursor.image_id))) return null;
    const card = state.galleryItems.find(item => String(item.id) === id);
    return reuseImageMedia({
      ...(card || {}), id, created_at: cursor.created_at, lightweight: true,
      gallery_revision: session.sequenceRevision, _manifestPending: true, references: [],
      images: entries.map(item => ({ ...item, id: item.image_id, ordinal: item.image_index })),
    });
  }

  async function prepareDetailTarget(cursor, session) {
    if (!cursor || !currentDetailSession(session)) return null;
    const epoch = session.epoch;
    const id = String(cursor.generation_id);
    let detail = String(state.detailId) === id ? state.detailData : session.summaries.get(id);
    if (!detail) detail = provisionalDetail(cursor, session) || await readDetailSummary(id, session);
    // The preview can be shown independently of the group manifest. Even a
    // slow/failing speculative manifest must not hold up a cached image.
    if (detail._manifestPending) void readDetailSummary(id, session).catch(() => {});
    if (!currentDetailSession(session) || epoch !== session.epoch) return null;
    const index = detail.images?.findIndex((item) => String(item.id) === String(cursor.image_id));
    if (!(index >= 0)) throw new Error("目标图片已删除或不再可用，请刷新画廊后重试。");
    const image = detail.images[index];
    let src = image.thumbnail_data_url || image.data_url;
    if (!src) {
      src = await loadDetailPreview(detail, image, session);
    }
    if (!src) throw new Error("目标图片的预览暂时不可用。");
    await decodeDisplayImage(src);
    return currentDetailSession(session) && epoch === session.epoch ? { src, cursor, detail, index } : null;
  }

  async function prepareDetailNeighbor(direction, session = detailNavigationSession) {
    const cursor = detailNeighborCursor(direction, session);
    if (cursor) {
      void ensureDetailSequence(session).catch(() => {});
      return prepareDetailTarget(cursor, session);
    }
    await ensureDetailSequence(session);
    return prepareDetailTarget(detailNeighborCursor(direction, session), session);
  }

  async function warmDetailNeighbors(session = detailNavigationSession) {
    if (!currentDetailSession(session) || !state.detailData || mobileImageViewer || mobileViewerOpening) return;
    try {
      await ensureDetailSequence(session);
      if (!currentDetailSession(session) || mobileImageViewer || mobileViewerOpening) return;
      await Promise.all([-1, 1].map((direction) => prepareDetailTarget(detailNeighborCursor(direction, session), session).catch(() => null)));
    } catch { /* A failed speculative read is retried and explained only on navigation. */ }
  }

  function loadDetailManifest(detail, session) {
    const current = () => currentDetailSession(session) && state.detailData === detail;
    if (!current()) return;
    if (detail._manifestEpoch !== session.epoch) {
      delete detail._manifestReady; delete detail._manifestError; delete detail._manifestErrorRendered;
      detail._manifestEpoch = session.epoch;
    }
    const frame = els.drawerBody.querySelector(".detail-image-frame");
    const busy = frame?.dataset.detailSwipeState || state.detailNavigating || mobileViewerSession?.active;
    if (detail._manifestReady && !busy) {
      const summary = detail._manifestReady;
      const imageId = detail.images[state.detailImageIndex]?.id;
      const index = summary.images?.findIndex(image => String(image.id) === String(imageId));
      if (index >= 0) {
        state.detailRequestedImageIndex = index;
        if (state.imagePreviewContext?.type === "detail" && String(state.imagePreviewContext.generationId) === String(detail.id)) {
          state.imagePreviewItems = normalizePreviewItems(detailDisplayImages(summary), "", "");
          state.imagePreviewIndex = index; state.imagePreviewContext.imageIndex = index;
        }
        void renderDetail(summary, state.detailFallbackThumbnail);
        if (state.imagePreviewContext?.type === "detail" && String(state.imagePreviewContext.generationId) === String(detail.id) && !els.imagePreview.classList.contains("is-hidden")) renderImagePreview();
        return;
      }
      delete detail._manifestReady;
      detail._manifestError = "目标图片已删除或不再可用，请刷新画廊后重试。";
    }
    if (detail._manifestError && !busy) {
      if (!detail._manifestErrorRendered) {
        detail._manifestErrorRendered = true;
        void renderDetail(detail, state.detailFallbackThumbnail);
      }
      return;
    }
    if (detail._manifestReady || detail._manifestError) {
      scheduleDetailAssets(detail.id, detail, state.detailFallbackThumbnail); return;
    }
    if (detail._manifestTask) return;
    const epoch = session.epoch;
    detail._manifestTask = readDetailSummary(String(detail.id), session).then(summary => {
      if (current() && session.epoch === epoch) detail._manifestReady = summary;
    }).catch(error => {
      if (current() && session.epoch === epoch) detail._manifestError = errorMessage(error, "生成详情加载失败");
    }).finally(() => {
      delete detail._manifestTask;
      if (current()) scheduleDetailAssets(detail.id, detail, state.detailFallbackThumbnail);
    });
  }

  function scheduleDetailAssets(id, summary, fallbackThumbnail) {
    window.clearTimeout(detailAssetsTimer);
    const revision = detailRequestRevision;
    const run = () => {
      detailAssetsTimer = 0;
      if (revision !== detailRequestRevision || String(state.detailId) !== String(id)) return;
      if (mobileViewerSession?.active) return;
      const frame = els.drawerBody.querySelector(".detail-image-frame");
      if (frame?.dataset.detailSwipeState || state.detailNavigating) {
        detailAssetsTimer = window.setTimeout(run, 160); return;
      }
      void loadDetailAssets(id, summary, fallbackThumbnail);
    };
    detailAssetsTimer = window.setTimeout(run, 160);
  }

  function centerDetailFilmstrip(strip, smooth = false) {
    cancelAnimationFrame(detailFilmstripScrollFrame);
    if (!strip) return;
    detailFilmstripScrollFrame = requestAnimationFrame(() => {
      if (!strip.isConnected) return;
      const active = strip.querySelector('[aria-current="true"]');
      if (!active) return;
      const target = active.offsetLeft + active.offsetWidth / 2 - strip.clientWidth / 2;
      strip.scrollTo({ left: target, behavior: smooth && Math.abs(target - strip.scrollLeft) < strip.clientWidth && !window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto" });
    });
  }

  function transitionDetailBackdrop(frame) {
    if (!frame || frame.classList.contains("is-detail-swiping")) return;
    const index = Number(frame.querySelector("[data-detail-image]")?.dataset.detailImage);
    const item = state.detailData?.images?.[index];
    const source = item?.thumbnail_data_url || (index === 0 ? state.detailFallbackThumbnail : "");
    if (!source) return;
    const backdrop = frame.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
    if (!backdrop) return;
    if (backdrop.getAttribute("src") === source && backdrop.dataset.backdropState === "idle") return;
    void window.ImageStudioBackdrop.transition(backdrop, source, { opacity: .62, previousClass: "detail-backdrop-previous" });
  }

  function decodeDisplayImage(source) {
    if (!decodedDisplayImages.has(source)) {
      const image = new Image(); image.className = "detail-image"; image.src = source;
      const entry = { ready: false, promise: null };
      entry.promise = image.decode().then(() => {
        if (!image.naturalWidth || !image.naturalHeight) throw new Error("图片内容无法解码。");
        entry.ready = true; return image;
      }).catch((error) => {
        if (decodedDisplayImages.get(source) === entry) decodedDisplayImages.delete(source);
        discardImageMediaSource(source);
        throw error;
      });
      decodedDisplayImages.set(source, entry);
      while (decodedDisplayImages.size > 8) decodedDisplayImages.delete(decodedDisplayImages.keys().next().value);
    }
    return decodedDisplayImages.get(source).promise;
  }

  async function paintDetailImage(frame, item, index) {
    const revision = ++detailImagePaintRevision;
    if (!frame) return false;
    if (!item?.data_url) { frame.setAttribute("aria-busy", "true"); return false; }
    const image = frame.querySelector("[data-detail-image]");
    const target = item.data_url;
    const preview = decodedDisplayImages.get(target)?.ready ? target : item.thumbnail_data_url || target;
    const current = () => frame.isConnected && frame.dataset.generationId === String(state.detailId) && revision === detailImagePaintRevision && (!mobileViewerSession?.active || mobileViewerSession.preparingDetail);
    const publish = async (source) => {
      if (!current()) return false;
      const previousSource = image.getAttribute("src");
      if (previousSource !== source) image.src = source;
      // A decoded candidate does not guarantee Safari has selected it on the mounted image.
      try { await image.decode(); }
      catch (error) {
        if (current() && previousSource && image.getAttribute("src") === source) image.src = previousSource;
        throw error;
      }
      if (!current()) return false;
      image.dataset.detailImage = String(index); image.alt = `生成结果 ${index + 1}`;
      frame.querySelector(".detail-image-pending")?.remove(); frame.removeAttribute("aria-busy");
      transitionDetailBackdrop(frame); return true;
    };
    if (!image.getAttribute("src") || Number(image.dataset.detailImage) !== index) frame.setAttribute("aria-busy", "true");
    try {
      await decodeDisplayImage(preview);
      if (!await publish(preview)) return false;
      if (target !== preview) void decodeDisplayImage(target).then(() => publish(target)).catch(() => {});
      return true;
    } catch (error) {
      if (target !== preview) {
        try { await decodeDisplayImage(target); return await publish(target); } catch { /* Keep the previous visible image when both sources fail. */ }
      }
      if (current()) {
        frame.removeAttribute("aria-busy"); showNotice(errorMessage(error, "图片暂时无法显示"), "error");
        if (image.getAttribute("src") && Number(image.dataset.detailImage) !== index) {
          state.detailRequestedImageIndex = Number(image.dataset.detailImage);
          void renderDetail(state.detailData, state.detailFallbackThumbnail);
        }
      }
      return false;
    }
  }

  function createDetailImageFrame(generationId) {
    const frame = document.createElement("div"); frame.className = "detail-image-frame"; frame.dataset.generationId = generationId;
    frame.innerHTML = `<div class="detail-image-background" aria-hidden="true"><img class="detail-image-backdrop" ${detailBackdropSource ? `src="${escape(detailBackdropSource)}"` : ""} alt="" /></div><img class="detail-image" alt="生成结果" data-detail-image="0" /><div class="detail-image-pending">正在读取图片…</div><button class="detail-carousel-nav is-previous" data-detail-nav="-1" type="button" aria-label="查看上一张图片"><span aria-hidden="true">‹</span></button><button class="detail-carousel-nav is-next" data-detail-nav="1" type="button" aria-label="查看下一张图片"><span aria-hidden="true">›</span></button>`;
    const current = () => frame.isConnected && frame.dataset.generationId === String(state.detailId);
    window.ImageStudioDetailSwipe.bind(frame, {
      getNeighbor: (direction) => {
        if (!current() || frame.getAttribute("aria-busy") === "true") return null;
        return cachedDetailNeighbor(direction);
      },
      prepareNeighbor: (direction) => current() && frame.getAttribute("aria-busy") !== "true" ? prepareDetailNeighbor(direction) : null,
      navigate: (direction, target) => current() && frame.getAttribute("aria-busy") !== "true" ? navigateDetail(direction, target) : false,
    });
    frame.addEventListener("detail-swipe-start", () => {
      window.ImageStudioBackdrop.pause(frame.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)"));
    });
    frame.addEventListener("detail-swipe-end", () => {
      if (current()) transitionDetailBackdrop(frame);
    });
    frame.addEventListener("detail-swipe-error", (event) => showNotice(`图片切换失败：${errorMessage(event.detail?.error, "目标图片暂时无法读取")}`, "error"));
    frame.querySelector("[data-detail-image]").addEventListener("click", (event) => {
      if (!current() || frame.getAttribute("aria-busy") === "true" || !event.currentTarget.getAttribute("src")) return;
      const index = Number(event.currentTarget.dataset.detailImage);
      const images = detailDisplayImages(state.detailData || {}, state.detailFallbackThumbnail);
      openImagePreview(event.currentTarget.src, `生成结果 ${index + 1}`, images[index]?.download_filename, images, index, { type: "detail", generationId: state.detailId, imageIndex: index });
    });
    frame.querySelectorAll("[data-detail-nav]").forEach((button) => button.addEventListener("click", () => void navigateDetail(Number(button.dataset.detailNav))));
    return frame;
  }

  function mountDetailFilmstrip(detail, images, imageIndex, previousStrip, previousScroll) {
    const mount = els.drawerBody.querySelector("[data-detail-filmstrip-mount]");
    if (!mount) return;
    const key = JSON.stringify([detail.id, images.map((item, index) => item.id || item.sha256 || index)]);
    const reused = previousStrip?.dataset.filmstripKey === key;
    const strip = reused ? previousStrip : document.createElement("div");
    const scrollLeft = reused ? previousScroll : 0;
    if (!reused) {
      strip.className = "detail-filmstrip";
      strip.dataset.filmstripKey = key;
      strip.dataset.generationId = detail.id;
      strip.setAttribute("role", "group");
      strip.setAttribute("aria-label", "本组生成图片缩略图");
      strip.innerHTML = `<div class="detail-filmstrip-track">${images.map((item, index) => {
        const preview = item.thumbnail_data_url || (index === 0 ? state.detailFallbackThumbnail : "");
        return `<button class="detail-filmstrip-thumb" data-detail-dot="${index}" type="button" aria-label="查看本次生成的第 ${index + 1} 张图片" title="第 ${index + 1} / ${images.length} 张"><span class="detail-filmstrip-preview"><span class="detail-filmstrip-placeholder" aria-hidden="true" ${preview ? "hidden" : ""}>${index + 1}</span>${preview ? `<img src="${escape(preview)}" alt="" draggable="false" loading="lazy" decoding="async" />` : ""}</span></button>`;
      }).join("")}</div>`;
      strip.querySelectorAll("img").forEach((image) => image.addEventListener("error", () => { image.hidden = true; image.parentElement.querySelector(".detail-filmstrip-placeholder").hidden = false; }));
      const selectImage = (index, focus = false) => {
        if (state.detailId !== strip.dataset.generationId || !state.detailData) return;
        if (index !== state.detailImageIndex) {
          state.detailRequestedImageIndex = index;
          renderDetail(state.detailData, state.detailFallbackThumbnail);
        } else centerDetailFilmstrip(strip, true);
        if (focus) strip.querySelector(`[data-detail-dot="${index}"]`)?.focus({ preventScroll: true });
      };
      strip.addEventListener("click", (event) => {
        const button = event.target.closest("[data-detail-dot]");
        if (button) selectImage(Number(button.dataset.detailDot));
      });
      strip.addEventListener("keydown", (event) => {
        const button = event.target.closest("[data-detail-dot]");
        if (!button) return;
        const index = Number(button.dataset.detailDot);
        const target = ({ ArrowLeft: Math.max(0, index - 1), ArrowRight: Math.min(images.length - 1, index + 1), Home: 0, End: images.length - 1 })[event.key];
        if (target === undefined) return;
        event.preventDefault(); event.stopPropagation(); selectImage(target, true);
      });
      strip.addEventListener("wheel", (event) => {
        if (event.ctrlKey || event.metaKey || strip.scrollWidth <= strip.clientWidth || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
        const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? strip.clientWidth : 1;
        event.preventDefault(); strip.scrollLeft += event.deltaY * scale;
      }, { passive: false });
    }
    mount.replaceWith(strip);
    strip.scrollLeft = scrollLeft;
    strip.querySelectorAll("[data-detail-dot]").forEach((button) => {
      const active = Number(button.dataset.detailDot) === imageIndex;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-current", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    centerDetailFilmstrip(strip, reused);
    watchDetailPreviews(detail, strip);
  }

  function renderDetail(detail, fallbackThumbnail = "") {
    reuseImageMedia(detail);
    const scrollTop = els.drawerBody.scrollTop;
    const images = Array.isArray(detail.images) ? detail.images : [];
    const displayImages = detailDisplayImages(detail, fallbackThumbnail);
    const requestedIndex = state.detailRequestedImageIndex < 0 ? displayImages.length - 1 : state.detailRequestedImageIndex;
    const imageIndex = displayImages.length ? Math.max(0, Math.min(displayImages.length - 1, requestedIndex)) : 0;
    const currentImage = displayImages[imageIndex] || null;
    const currentImageUrl = currentImage?.data_url || "";
    state.detailImageIndex = imageIndex; state.detailData = detail;
    if (detail.lightweight) state.detailAssetsLoaded = !!detail.images[imageIndex]?._originalLoaded;
    if (state.detailAssetsLoaded) state.detailRequestedImageIndex = imageIndex;
    const refs = Array.isArray(detail.references) ? detail.references : [];
    const totalBytes = images.reduce((sum, item) => sum + Number(item.size_bytes || 0), 0);
    els.detailDate.textContent = formatDate(detail.created_at);
    const sourceIdentity = invocationSourceLabel(detail.invocation_source);
    const canPrevious = imageIndex > 0 || hasAdjacentGalleryRecord(-1);
    const canNext = imageIndex < displayImages.length - 1 || hasAdjacentGalleryRecord(1);
    const previousStrip = els.drawerBody.querySelector(".detail-filmstrip");
    const previousStripScroll = previousStrip?.scrollLeft || 0;
    const focusedThumbnail = previousStrip?.contains(document.activeElement);
    const stripMount = displayImages.length > 1 ? '<div data-detail-filmstrip-mount></div>' : "";
    const previousFrame = els.drawerBody.querySelector(".detail-image-frame");
    const imageFrame = currentImage ? previousFrame || createDetailImageFrame(detail.id) : null;
    if (imageFrame) imageFrame.dataset.generationId = String(detail.id);
    const carousel = currentImage ? `<div class="detail-images"><div data-detail-frame-mount></div>${stripMount}</div>` : '<div class="detail-loading">正在读取生成图片…</div>';
    const metadata = detail._manifestPending
      ? `<div class="detail-block detail-manifest-loading" role="status"><p>${detail._manifestError ? escape(detail._manifestError) : "正在读取本组生成详情…"}</p>${detail._manifestError ? '<button class="quiet-button" data-detail-manifest-retry type="button">重试读取详情</button>' : ""}</div>`
      : `${library.detailMetadataMarkup(detail, currentImage)}<div class="detail-block"><h3>信息</h3><pre>${escape(JSON.stringify({ 服务商: detail.provider_name, 模型: detail.model, 模式: library.modeLabel(detail.mode), 生图来源: library.engineLabel(detail.generation_engine), 来源: sourceLabel(detail.source), 调用来源身份: sourceIdentity, 记录时间: formatDate(detail.created_at), 图片数量: images.length, 文件大小: formatBytes(totalBytes), 耗时毫秒: detail.elapsed_ms }, null, 2))}</pre></div><div class="detail-block"><h3>参考图</h3><div class="detail-references" data-detail-references>${detailReferenceMarkup(refs)}</div></div>${library.detailWarningsMarkup(detail, currentImage)}`;
    // The foreground and fixed backdrop stay attached across both image and group changes.
    if (imageFrame && imageFrame === previousFrame) {
      const media = imageFrame.parentElement;
      while (media.nextSibling) media.nextSibling.remove();
      previousStrip?.remove();
      media.insertAdjacentHTML("beforeend", stripMount);
      els.drawerBody.insertAdjacentHTML("beforeend", metadata);
    } else {
      els.drawerBody.innerHTML = carousel + metadata;
      if (imageFrame) els.drawerBody.querySelector("[data-detail-frame-mount]").replaceWith(imageFrame);
    }
    if (imageFrame) {
      imageFrame.querySelector('[data-detail-nav="-1"]').disabled = !canPrevious;
      imageFrame.querySelector('[data-detail-nav="1"]').disabled = !canNext;
    }
    mountDetailFilmstrip(detail, displayImages, imageIndex, previousStrip, previousStripScroll);
    if (focusedThumbnail) els.drawerBody.querySelector(`.detail-filmstrip [data-detail-dot="${imageIndex}"]`)?.focus({ preventScroll: true });
    library.updateDetailActions(detail);
    library.layoutDetailParameters();
    bindDetailReferenceEvents(detail);
    watchDetailReferences(detail);
    els.drawerBody.querySelector('[data-detail-manifest-retry]')?.addEventListener("click", () => {
      delete detail._manifestError; delete detail._manifestErrorRendered;
      detailNavigationSession?.summaries.delete(String(detail.id));
      void renderDetail(detail, fallbackThumbnail);
    });
    els.drawerBody.querySelector('[data-detail-metadata-retry]')?.addEventListener("click", () => { const image = detail.images[state.detailImageIndex]; delete image._metadataError; void loadDetailAssets(detail.id, detail, fallbackThumbnail); });
    els.drawerBody.querySelector("[data-reproduce]")?.addEventListener("click", () => void reproduce(detail.id));
    els.drawerBody.querySelector("[data-copy-request]")?.addEventListener("click", () => void copyRequestParameters(detail));
    els.drawerBody.querySelector("[data-output-reference]")?.addEventListener("click", () => void useDataUrlAsReference(currentImageUrl, "gallery-output-reference.png"));
    els.drawerBody.scrollTop = scrollTop;
    if (detail.lightweight && !mobileViewerSession?.active) void loadDetailAssets(detail.id, detail, fallbackThumbnail);
    return paintDetailImage(imageFrame, currentImage, imageIndex).then((painted) => {
      if (painted && String(state.detailId) === String(detail.id)) void warmDetailNeighbors();
      return painted;
    });
  }

  async function loadDetailAssets(id, summary, fallbackThumbnail) {
    if (summary._manifestPending) {
      const session = detailNavigationSession, image = summary.images?.[state.detailImageIndex];
      if (!image || !isCurrentDetailImage(summary, image, session)) return;
      const current = () => isCurrentDetailImage(summary, image, session) && !mobileViewerSession?.active;
      // Filmstrip selection can jump to a preview that was not preloaded.
      // It must remain independent of the still-pending group manifest too.
      if (!image.thumbnail_data_url && !image._previewTask) image._previewTask = loadDetailPreview(summary, image, session, current).then(source => {
        if (source && current()) void paintDetailImage(els.drawerBody.querySelector(".detail-image-frame"), detailDisplayImages(summary, fallbackThumbnail)[state.detailImageIndex], state.detailImageIndex);
      }).catch(error => {
        if (!current()) return;
        els.drawerBody.querySelector(".detail-image-frame")?.removeAttribute("aria-busy");
        showNotice(errorMessage(error, "图片预览加载失败"), "error");
      }).finally(() => { delete image._previewTask; });
      // Do not hydrate synchronously inside renderDetail: its pending preview
      // paint would otherwise run after the hydrated group's newer paint.
      queueMicrotask(() => loadDetailManifest(summary, session)); return;
    }
    if (summary.lightweight) {
      const session = detailNavigationSession;
      const image = summary.images?.[state.detailImageIndex];
      if (!image || !isCurrentDetailImage(summary, image, session) || mobileViewerSession?.active) return;
      const current = () => isCurrentDetailImage(summary, image, session) && !mobileViewerSession?.active;
      const loadEpoch = session.epoch;
      const settled = key => {
        delete image[key];
        // A gallery mutation can supersede a pending read. Resume the selected
        // image once, using the new epoch, instead of leaving its shell loading.
        if (current() && session.epoch !== loadEpoch) void loadDetailAssets(id, summary, fallbackThumbnail);
      };
      const paint = () => {
        if (!current()) return;
        state.detailAssetsLoaded = !!image._originalLoaded;
        library.updateDetailActions(summary);
        void paintDetailImage(els.drawerBody.querySelector(".detail-image-frame"), detailDisplayImages(summary, fallbackThumbnail)[state.detailImageIndex], state.detailImageIndex);
        if (state.imagePreviewContext?.type === "detail" && state.imagePreviewContext.generationId === id && state.imagePreviewContext.imageIndex === state.detailImageIndex && !els.imagePreview.classList.contains("is-hidden")) {
          Object.assign(state.imagePreviewItems[state.imagePreviewIndex], image);
          renderImagePreview();
        }
      };
      if (!image.thumbnail_data_url && !image._previewTask) image._previewTask = loadDetailPreview(summary, image, session, current).then(paint).catch(() => {}).finally(() => settled("_previewTask"));
      if (!image._originalLoaded && !image._originalError && !image._originalTask) image._originalTask = loadDetailOriginal(summary, image, session).then(paint).catch(error => {
        if (!current() || session.epoch !== loadEpoch) return;
        image._originalError = errorMessage(error, "原图加载失败"); showNotice(image._originalError, "error");
      }).finally(() => settled("_originalTask"));
      if (!image._metadataLoaded && !image._metadataError && !image._metadataTask) image._metadataTask = loadDetailMetadata(summary, image, session).then(result => {
        if (result && current()) void renderDetail(summary, fallbackThumbnail);
      }).catch(error => {
        if (!current() || session.epoch !== loadEpoch) return;
        image._metadataError = errorMessage(error, "图片参数加载失败"); void renderDetail(summary, fallbackThumbnail);
      }).finally(() => settled("_metadataTask"));
      return;
    }
    const revision = detailRequestRevision;
    try {
      const assets = await apiGet(`gallery/assets/${id}`);
      if (revision !== detailRequestRevision || state.detailId !== id) return;
      const summaryImages = Array.isArray(summary.images) ? summary.images : [];
      const summaryById = new Map(summaryImages.filter((image) => image.id).map((image) => [String(image.id), image]));
      const assetImages = Array.isArray(assets.images) ? assets.images : [];
      const mergedImages = assetImages.map((item, index) => ({
        ...(item.id ? summaryById.get(String(item.id)) : summaryImages[index]),
        ...item,
        thumbnail_data_url: item?.thumbnail_data_url || (item.id ? summaryById.get(String(item.id)) : summaryImages[index])?.thumbnail_data_url || "",
      }));
      state.detailAssetsLoaded = true;
      state.detailData = { ...summary, ...assets, images: mergedImages };
      if (mobileViewerSession?.active) return;
      library.updateDetailActions(state.detailData);
      if (state.imagePreviewContext?.type === "detail" && state.imagePreviewContext.generationId === id) {
        const previewItems = normalizePreviewItems(detailDisplayImages(state.detailData, fallbackThumbnail), "", "");
        if (previewItems.length) {
          state.imagePreviewItems = previewItems;
          state.imagePreviewIndex = Math.max(0, Math.min(previewItems.length - 1, state.imagePreviewContext.imageIndex || 0));
          state.imagePreviewContext.imageIndex = state.imagePreviewIndex;
          if (!els.imagePreview.classList.contains("is-hidden")) renderImagePreview();
        }
      }
      const currentImage = detailDisplayImages(state.detailData, fallbackThumbnail)[state.detailImageIndex];
      const currentImageUrl = currentImage?.data_url || "";
      void paintDetailImage(els.drawerBody.querySelector(".detail-image-frame"), currentImage, state.detailImageIndex);
      const references = els.drawerBody.querySelector("[data-detail-references]");
      if (references) {
        const refs = Array.isArray(state.detailData.references) ? state.detailData.references : [];
        references.innerHTML = detailReferenceMarkup(refs);
        bindDetailReferenceEvents(state.detailData);
      }
      const placeholder = els.drawerBody.querySelector("[data-detail-assets-placeholder]");
      if (placeholder && currentImageUrl) {
        const referenceButton = document.createElement("button");
        referenceButton.className = "quiet-button";
        referenceButton.type = "button";
        referenceButton.dataset.outputReference = "1";
        referenceButton.textContent = "将当前成图用作新参考图";
        referenceButton.addEventListener("click", () => void useDataUrlAsReference(currentImageUrl, "gallery-output-reference.png"));
        placeholder.replaceWith(referenceButton);
      }
    } catch (error) {
      if (revision === detailRequestRevision && state.detailId === id) showNotice(errorMessage(error, "高清图片加载失败"), "error");
    }
  }

  async function openDetail(id, requestedImageIndex = 0, options = {}) {
    library.clearDetailParameterLayout();
    const session = createDetailNavigation();
    state.detailNavigating = false;
    const previousBackdrop = els.drawerBody.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
    if (state.detailId) detailBackdropSource = previousBackdrop?.getAttribute("src") || detailBackdropSource;
    if (previousBackdrop) window.ImageStudioBackdrop.dispose(previousBackdrop);
    detailImagePaintRevision++;
    const revision = ++detailRequestRevision;
    state.detailId = id; state.detailData = null; state.detailAssetsLoaded = false; state.detailRequestedImageIndex = requestedImageIndex === "last" ? -1 : Math.max(0, Number(requestedImageIndex) || 0);
    library.updateDetailActions(null);
    const card = state.galleryItems.find((item) => String(item.id) === String(id));
    state.detailFallbackThumbnail = card?.thumbnail_data_url || "";
    els.detailDrawer.classList.add("is-open"); els.detailDrawer.setAttribute("aria-hidden", "false"); els.scrim.classList.remove("is-hidden"); syncPageScrollLock(); if (options.focus !== false) els.detailDrawer.focus();
    els.detailDate.textContent = "";
    if (options.resetScroll !== false) els.drawerBody.scrollTop = 0; els.drawerBody.innerHTML = '<div class="detail-loading">正在读取生成详情…</div>';
    try {
      const summary = await apiGet(`gallery/detail/${id}`, { light: "1" });
      if (revision !== detailRequestRevision || state.detailId !== id) return;
      observeGalleryRevision(summary.gallery_revision);
      if (options.imageId) {
        const index = summary.images?.findIndex(image => String(image.id) === String(options.imageId));
        if (!(index >= 0)) throw new Error("目标图片已删除或不再可用，请刷新画廊后重试。");
        state.detailRequestedImageIndex = index;
      }
      state.detailData = summary; await renderDetail(summary, state.detailFallbackThumbnail);
      if (revision !== detailRequestRevision || state.detailId !== id) return;
      void warmDetailNeighbors(session);
      if (!options.deferAssets) void loadDetailAssets(id, summary, state.detailFallbackThumbnail);
    } catch (error) { if (revision === detailRequestRevision && state.detailId === id) showNotice(errorMessage(error, "生成详情加载失败"), "error"); }
  }

  async function navigateDetail(direction, preparedTarget = null) {
    if (state.detailNavigating || !state.detailData || mobileImageViewer || mobileViewerOpening) return false;
    const session = detailNavigationSession;
    const epoch = session?.epoch;
    const originId = state.detailId;
    const originIndex = state.detailImageIndex;
    const originImageId = state.detailData.images?.[originIndex]?.id;
    const revision = detailRequestRevision;
    state.detailNavigating = true;
    try {
      const target = preparedTarget?.detail ? preparedTarget : await prepareDetailNeighbor(direction, session);
      if (!target || !currentDetailSession(session) || session.epoch !== epoch || revision !== detailRequestRevision || state.detailId !== originId || state.detailData.images?.[state.detailImageIndex]?.id !== originImageId || mobileImageViewer || mobileViewerOpening) return false;
      const expected = detailNeighborCursor(direction, session);
      if (!expected || String(expected.image_id) !== String(target.cursor.image_id)) return false;
      const crossGroup = String(target.cursor.generation_id) !== String(originId);
      if (crossGroup) {
        if (!state.detailData._manifestPending) {
          session.summaries.set(String(originId), state.detailData);
          while (session.summaries.size > 3) session.summaries.delete(session.summaries.keys().next().value);
        }
        detailRequestRevision++; detailImagePaintRevision++;
        state.detailId = String(target.cursor.generation_id); state.detailAssetsLoaded = false;
        state.detailFallbackThumbnail = target.detail.images?.[0]?.thumbnail_data_url || "";
      }
      state.detailRequestedImageIndex = target.index;
      const detail = crossGroup ? target.detail : state.detailData;
      const painted = await renderDetail(detail, state.detailFallbackThumbnail);
      if (painted && crossGroup && currentDetailSession(session) && String(state.detailId) === String(target.cursor.generation_id)) scheduleDetailAssets(state.detailId, detail, state.detailFallbackThumbnail);
      return painted;
    } catch (error) {
      if (currentDetailSession(session) && revision === detailRequestRevision) showNotice(`图片切换失败：${errorMessage(error, "目标图片暂时无法读取")}`, "error");
      return false;
    } finally { if (currentDetailSession(session)) state.detailNavigating = false; }
  }

  function closeDetail() { library.clearDetailParameterLayout(); stopDetailNavigation(); window.ImageStudioBackdrop.dispose(els.drawerBody.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)")); detailImagePaintRevision++; mobileDetailSyncRevision++; decodedDisplayImages.clear(); detailBackdropSource = ""; detailRequestRevision += 1; if (mobileImageViewer) { suppressMobileDetailSync = true; mobileImageViewer.close(); } state.detailId = ""; state.detailData = null; state.detailFallbackThumbnail = ""; state.detailAssetsLoaded = false; state.detailNavigating = false; state.detailImageIndex = 0; state.detailRequestedImageIndex = 0; closeImagePreview(); els.detailDrawer.classList.remove("is-open"); els.detailDrawer.setAttribute("aria-hidden", "true"); if (!activeConfirmation) els.scrim.classList.add("is-hidden"); syncPageScrollLock(); }

  function isMobileDetailPreview(context) {
    return context?.type === "detail" && window.matchMedia("(max-width: 540px)").matches && typeof window.PhotoSwipe === "function";
  }

  function mobileSourceForSequenceItem(sequenceItem, fallbackDataUrl, context, knownImages, galleryById) {
    const cachedPreview = getImageMedia(sequenceItem, "preview");
    if (cachedPreview) return { src: cachedPreview, detail: "preview" };
    const sameRecord = String(state.detailId) === String(sequenceItem.generation_id);
    const known = sameRecord ? knownImages.get(String(sequenceItem.image_id)) || knownImages.get(`index:${sequenceItem.image_index}`) : null;
    if (known?.thumbnail_data_url) return { src: known.thumbnail_data_url, detail: "preview" };
    if (known?.data_url) return { src: known.data_url, detail: known._originalLoaded || (!state.detailData?.lightweight && state.detailAssetsLoaded) ? "original" : "preview" };
    if (String(sequenceItem.generation_id) === String(context.generationId) && sequenceItem.image_index === context.imageIndex && fallbackDataUrl) return { src: fallbackDataUrl, detail: state.detailAssetsLoaded ? "original" : "preview" };
    const card = sequenceItem.image_index === 0 ? galleryById.get(String(sequenceItem.generation_id)) : null;
    if (card?.thumbnail_data_url) return { src: card.thumbnail_data_url, detail: "preview" };
    return { src: EMPTY_MOBILE_IMAGE, detail: "" };
  }

  function isCurrentMobileSession(session, index, item) {
    return !!session?.active && !session.viewer.isDestroying && mobileViewerSession === session && mobileImageViewer === session.viewer && (!item || session.items[index] === item);
  }

  function mobileViewerBusy(session) {
    const viewer = session.viewer;
    return document.hidden || session.pointerIds.size > 0 || session.touchCount > 0 || viewer.opener.isOpening
      || viewer.gestures.isDragging || viewer.gestures.isZooming || viewer.mainScroll.isShifted()
      || viewer.animations.activeAnimations.some((animation) => animation.props.isMainScroll || animation.props.isPan);
  }

  function scheduleMobileWork(session) {
    if (!isCurrentMobileSession(session) || session.workTimer || !session.workQueue.size) return;
    session.workTimer = window.setTimeout(() => {
      session.workTimer = 0;
      if (!isCurrentMobileSession(session)) return;
      if (mobileViewerBusy(session)) { scheduleMobileWork(session); return; }
      const jobs = Array.from(session.workQueue.values()); session.workQueue.clear();
      jobs.forEach((job) => { Promise.resolve().then(job.run).then(job.resolve, job.reject); });
    }, 80);
  }

  function queueMobileWork(session, key, run) {
    if (!isCurrentMobileSession(session)) return Promise.resolve();
    const previous = session.workQueue.get(key);
    if (previous) return previous.promise;
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    session.workQueue.set(key, { run, resolve, reject, promise }); scheduleMobileWork(session);
    return promise;
  }

  function cancelMobileWork(session) {
    window.clearTimeout(session.workTimer); session.workTimer = 0;
    session.workQueue.forEach((job) => job.resolve()); session.workQueue.clear();
    session.entryAnimation?.cancel();
    session.retryTimers.forEach((timer) => window.clearTimeout(timer)); session.retryTimers.clear();
  }

  function trackMobilePointer(session, event, down) {
    const original = event.originalEvent;
    if (original?.pointerId !== undefined) down ? session.pointerIds.add(original.pointerId) : session.pointerIds.delete(original.pointerId);
    else session.touchCount = original?.touches?.length ?? (down ? 1 : 0);
    if (down) {
      session.entryAnimation?.cancel();
      window.clearTimeout(session.workTimer); session.workTimer = 0;
      window.ImageStudioBackdrop.pause(session.backdrop);
    } else {
      updateMobileViewerFeedback(session); scheduleMobileWork(session);
    }
  }

  function updateMobileImageSource(index, payload, detail, session = mobileViewerSession) {
    const item = session?.items[index];
    if (!isCurrentMobileSession(session, index, item) || !item || !payload?.data_url) return;
    if (payload.id && String(payload.id) !== String(item.image_id)) return;
    if (detail === "original" && session.viewer.currIndex !== index) return;
    if (detail === "original") { item.originalSrc = payload.data_url; session.originalIndices.add(index); }
    else item.previewSrc = payload.data_url;
    if (detail === "original") { item.originalError = ""; item.previewError = ""; item.originalRetryCount = 0; }
    else item.previewError = "";
    const source = item.originalSrc || item.previewSrc || payload.data_url;
    item.src = source; item.msrc = item.previewSrc || source; item.loadedDetail = item.originalSrc ? "original" : "preview";
    const contents = new Set();
    const cached = session.viewer.contentLoader?.getContentByIndex(index);
    if (cached) contents.add(cached);
    const holders = session.viewer.mainScroll?.itemHolders || [];
    holders.forEach((holder) => {
      const slide = holder.slide;
      if (!slide || slide.index !== index) return;
      slide.data.src = source; slide.data.msrc = item.msrc;
      if (slide.content) contents.add(slide.content);
    });
    contents.forEach((content) => {
      if (content.data.image_id && String(content.data.image_id) !== String(item.image_id)) return;
      content.data.src = source; content.data.msrc = item.msrc;
      if (content.isError() || content.element?.tagName !== "IMG") {
        // Error content is a DIV; reload through PhotoSwipe instead of changing DOM src.
        content.remove(); content.load(false, true);
        if (content.hasSlide) content.append();
      } else if (content.element.getAttribute("src") !== source) {
        content.loadImage(false);
      }
    });
    if (session.viewer.currIndex === index) updateMobileViewerFeedback(session);
  }

  async function loadMobileImage(index, detail, session = mobileViewerSession) {
    const item = session?.items[index];
    if (!item || !isCurrentMobileSession(session, index, item)) return;
    const key = `${item.image_id}:${detail}`;
    const loads = session.loads;
    if (!loads.has(key)) {
      const relevant = () => isCurrentMobileSession(session, index, item)
        && Math.abs(session.viewer.currIndex - index) <= (detail === "original" ? 0 : 1);
      const pending = queueMobileWork(session, `load:${key}`, async () => {
        if (!relevant()) return;
        const available = detail === "original" ? item.originalSrc : item.previewSrc;
        const known = detail === "original" && String(state.detailId) === String(item.generation_id)
          ? state.detailData?.images?.find((image) => String(image.id) === String(item.image_id)) : null;
        const knownOriginal = known?._originalLoaded || (!state.detailData?.lightweight && state.detailAssetsLoaded) ? known?.data_url : "";
        const source = available || knownOriginal || await loadImageMedia(item, detail);
        const payload = { id: item.image_id, data_url: source };
        if (source) cacheImageMedia(item, detail, source);
        if (!relevant()) return;
        if (!payload?.data_url) throw new Error("图片接口没有返回可用内容。");
        // A response or decode may finish during a later gesture; gate both stages separately.
        await queueMobileWork(session, `decode:${key}`, () => relevant() ? decodeDisplayImage(payload.data_url) : undefined);
        await queueMobileWork(session, `paint:${key}`, () => { if (relevant()) updateMobileImageSource(index, payload, detail, session); });
        return payload;
      }).finally(() => { if (loads.get(key) === pending) loads.delete(key); });
      loads.set(key, pending);
    }
    await loads.get(key);
  }

  function pruneMobileOriginals(currentIndex, session = mobileViewerSession) {
    if (!isCurrentMobileSession(session)) return;
    session.originalIndices.forEach((index) => {
      if (Math.abs(index - currentIndex) <= 1) return;
      session.originalIndices.delete(index);
      const item = session.items[index];
      item.originalSrc = "";
      item.src = item.previewSrc || EMPTY_MOBILE_IMAGE;
      item.msrc = item.src;
      item.loadedDetail = item.previewSrc ? "preview" : "";
      const cached = session.viewer.contentLoader?.getContentByIndex(index);
      if (cached && !cached.hasSlide && !cached.isAttached) { session.viewer.contentLoader.removeByIndex(index); cached.destroy(); }
    });
  }

  function updateMobileViewerFeedback(session = mobileViewerSession) {
    if (!isCurrentMobileSession(session)) return;
    void queueMobileWork(session, "feedback", () => applyMobileViewerFeedback(session));
  }

  function applyMobileViewerFeedback(session) {
    if (!isCurrentMobileSession(session)) return;
    const item = session.items[session.viewer.currIndex];
    const src = item?.previewSrc || "";
    if (session.backdrop) void window.ImageStudioBackdrop.transition(session.backdrop, src, { opacity: .64, previousClass: "image-studio-viewer-backdrop-previous" });
    const message = item?.originalSrc ? "" : item?.originalError || item?.previewError || "";
    if (session.status) {
      session.status.hidden = !message;
      session.status.querySelector("span").textContent = item?.previewSrc && !item.originalSrc ? "原图加载失败，当前显示预览。" : "图片暂时无法加载。";
      session.status.querySelector("button").disabled = !!session.retrying;
    }
  }

  async function retryMobileImage(session = mobileViewerSession, index = session?.viewer.currIndex) {
    if (!isCurrentMobileSession(session) || session.retrying || !session.items[index]) return;
    const item = session.items[index]; session.retrying = true; updateMobileViewerFeedback(session);
    const content = session.viewer.contentLoader?.getContentByIndex(index);
    if (content?.isError() || content?.element?.tagName === "DIV") {
      item.originalSrc = ""; item.previewSrc = ""; item.src = EMPTY_MOBILE_IMAGE;
    }
    try {
      await loadMobileImage(index, "preview", session).catch(() => {});
      await loadMobileImage(index, "original", session);
    } catch (error) { if (isCurrentMobileSession(session, index, item)) item.originalError = errorMessage(error, "原图加载失败"); }
    finally { session.retrying = false; updateMobileViewerFeedback(session); }
  }

  function warmMobileImages(index, session = mobileViewerSession) {
    if (!isCurrentMobileSession(session)) return;
    void queueMobileWork(session, "prune", () => pruneMobileOriginals(session.viewer.currIndex, session));
    updateMobileViewerFeedback(session);
    for (const neighbor of [index - 1, index, index + 1]) {
      if (neighbor >= 0 && neighbor < session.items.length) void loadMobileImage(neighbor, "preview", session).catch((error) => {
        if (!isCurrentMobileSession(session)) return;
        session.items[neighbor].previewError = errorMessage(error, "预览加载失败"); updateMobileViewerFeedback(session);
      });
    }
    void loadMobileImage(index, "original", session).catch((error) => {
      if (!isCurrentMobileSession(session)) return;
      const item = session.items[index]; item.originalError = errorMessage(error, "原图加载失败"); updateMobileViewerFeedback(session);
      if (!item.originalRetryCount) {
        item.originalRetryCount = 1;
        const timer = window.setTimeout(() => { session.retryTimers.delete(timer); if (isCurrentMobileSession(session) && session.viewer.currIndex === index) void retryMobileImage(session, index); }, 650);
        session.retryTimers.add(timer);
      }
    });
  }

  async function syncDetailToMobileImage(item, session = null) {
    if (!item) return;
    const current = () => session ? session.active && mobileViewerSession === session && !suppressMobileDetailSync : !mobileViewerSession;
    if (!current()) return;
    const revision = ++mobileDetailSyncRevision;
    const detailRevision = detailRequestRevision;
    const targetPage = Math.floor(Math.max(0, Number(item.generation_position || 0)) / state.galleryLimit);
    if (targetPage !== state.galleryPage && !await loadGallery(targetPage)) return;
    if (revision !== mobileDetailSyncRevision || detailRevision !== detailRequestRevision || !current()) return;
    const imageIndex = state.detailData?.images?.findIndex(image => String(image.id) === String(item.image_id));
    const expectedRevision = session?.sequenceRevision || galleryDataRevision;
    const staleManifest = expectedRevision && state.detailData?.gallery_revision !== expectedRevision;
    if (String(state.detailId) !== String(item.generation_id) || !state.detailData || !(imageIndex >= 0) || staleManifest) {
      await openDetail(item.generation_id, item.image_index, { focus: false, resetScroll: false, deferAssets: true, imageId: item.image_id });
      return;
    }
    state.detailRequestedImageIndex = imageIndex;
    await renderDetail(state.detailData, state.detailFallbackThumbnail);
  }

  function prepareMobileDetail(session) {
    if (!session.active || mobileViewerSession !== session || suppressMobileDetailSync) return Promise.resolve();
    const index = session.viewer.currIndex;
    if (session.detailSyncIndex === index && session.detailSyncPromise) return session.detailSyncPromise;
    session.detailSyncIndex = index; session.preparingDetail = true;
    const item = session.sequence[index];
    if (String(state.detailId) !== String(item.generation_id)) {
      library.updateDetailActions(null);
      els.drawerBody.innerHTML = '<div class="detail-loading">正在读取生成详情…</div>';
    }
    const promise = syncDetailToMobileImage(item, session).catch((error) => {
      if (session.active && mobileViewerSession === session) showNotice(errorMessage(error, "生成详情同步失败"), "error");
    }).finally(() => { if (session.detailSyncPromise === promise) session.preparingDetail = false; });
    session.detailSyncPromise = promise;
    return promise;
  }

  async function downloadMobileImage() {
    const item = mobileImageSequence[mobileImageViewer?.currIndex ?? -1];
    if (!item) return;
    try {
      const client = await bridge();
      await client.download(`gallery/download/${item.image_id}`, {}, item.download_filename);
    } catch (error) {
      showNotice(errorMessage(error, "图片下载失败"), "error");
    }
  }

  function toggleMobileImageControls() {
    mobileImageViewer?.element?.classList.toggle("image-studio-controls-visible");
  }

  async function openMobileImageViewer(dataUrl, context) {
    if (mobileImageViewer || mobileViewerOpening) return true;
    mobileViewerOpening = true;
    const openingRevision = ++mobileViewerOpenRevision;
    const requestedDetailRevision = detailRequestRevision;
    const requestedImageId = state.detailData?.images?.[context.imageIndex]?.id;
    try {
      const sequence = await ensureDetailSequence(detailNavigationSession);
      if (openingRevision !== mobileViewerOpenRevision || requestedDetailRevision !== detailRequestRevision || String(state.detailId) !== String(context.generationId)) return true;
      const initialIndex = sequence.findIndex((item) => String(item.generation_id) === String(context.generationId) && (requestedImageId ? String(item.image_id) === String(requestedImageId) : Number(item.image_index) === Number(context.imageIndex)));
      if (initialIndex < 0 || !sequence.length) return false;
      mobileDetailSyncRevision++;
      mobileImageSequence = sequence;
      mobileImageLoads = new Map();
      const knownImages = new Map(detailDisplayImages(state.detailData || {}, state.detailFallbackThumbnail).map((item, index) => [item.id ? String(item.id) : `index:${index}`, item]));
      const galleryById = new Map(state.galleryItems.map((item) => [String(item.id), item]));
      mobileImageDataSource = sequence.map((item) => {
        const known = mobileSourceForSequenceItem(item, dataUrl, context, knownImages, galleryById);
        const preview = known.detail === "preview" ? known.src : "";
        const original = known.detail === "original" ? known.src : getImageMedia(item, "original");
        return { ...item, src: original || known.src, msrc: preview || original || known.src, width: Math.max(1, Number(item.width || 1)), height: Math.max(1, Number(item.height || 1)), previewSrc: preview, originalSrc: original, loadedDetail: original ? "original" : known.detail, alt: "生成结果" };
      });
      const session = { active: true, sequence, items: mobileImageDataSource, loads: mobileImageLoads, retryTimers: new Set(), viewer: null, backdrop: null, status: null, retrying: false, workQueue: new Map(), workTimer: 0, pointerIds: new Set(), touchCount: 0, originalIndices: new Set(mobileImageDataSource.flatMap((item, index) => item.originalSrc ? [index] : [])), preparingDetail: false, detailSyncIndex: -1, detailSyncPromise: null, entryAnimation: null };
      session.sequenceRevision = detailNavigationSession?.sequenceRevision || galleryDataRevision;
      // PhotoSwipe blocks input during its opening animation; the visual fade is independent.
      const pswp = new window.PhotoSwipe({ dataSource: mobileImageDataSource, index: initialIndex, loop: false, closeOnVerticalDrag: true, pinchToClose: false, tapAction: toggleMobileImageControls, imageClickAction: toggleMobileImageControls, bgClickAction: toggleMobileImageControls, doubleTapAction: "zoom", initialZoomLevel: "fit", secondaryZoomLevel: 2.5, maxZoomLevel: 4, preload: [1, 1], arrowPrev: false, arrowNext: false, close: false, zoom: false, counter: false, bgOpacity: 1, showHideAnimationType: "fade", showAnimationDuration: 0, hideAnimationDuration: 220, zoomAnimationDuration: 220, errorMsg: "图片暂时无法加载，请重试。", mainClass: "image-studio-pswp" });
      session.viewer = pswp;
      pswp.addFilter("contentErrorElement", (element, content) => {
        element.textContent = "图片暂时无法加载。";
        const retry = document.createElement("button"); retry.type = "button"; retry.className = "image-studio-image-retry"; retry.textContent = "重新加载";
        retry.addEventListener("click", (event) => { event.stopPropagation(); void retryMobileImage(session, content.index); });
        element.appendChild(retry); return element;
      });
      pswp.on("uiRegister", () => {
        pswp.ui.registerElement({ name: "image-studio-download", className: "pswp__button--image-studio-download", isButton: true, appendTo: "root", title: "下载图片", ariaLabel: "下载当前图片", html: '<svg class="image-studio-download-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>', onClick: () => void downloadMobileImage() });
      });
      pswp.on("change", () => {
        if (!isCurrentMobileSession(session)) return;
        if (session.preparingDetail) { mobileDetailSyncRevision++; detailRequestRevision++; detailImagePaintRevision++; }
        session.preparingDetail = false; session.detailSyncIndex = -1; session.detailSyncPromise = null;
        warmMobileImages(pswp.currIndex, session);
      });
      pswp.on("pointerDown", (event) => trackMobilePointer(session, event, true));
      pswp.on("pointerUp", (event) => trackMobilePointer(session, event, false));
      pswp.on("verticalDrag", () => { void prepareMobileDetail(session); });
      pswp.on("close", () => {
        cancelMobileWork(session);
        window.ImageStudioBackdrop.pause(session.backdrop);
        void prepareMobileDetail(session);
      });
      pswp.on("afterInit", () => {
        const backgroundLayer = document.createElement("div"); backgroundLayer.className = "image-studio-viewer-background"; backgroundLayer.setAttribute("aria-hidden", "true");
        const background = document.createElement("img"); background.className = "image-studio-viewer-backdrop"; background.alt = ""; background.setAttribute("aria-hidden", "true");
        backgroundLayer.appendChild(background); pswp.bg?.appendChild(backgroundLayer); session.backdrop = background;
        const status = document.createElement("div"); status.className = "image-studio-image-status"; status.hidden = true; status.setAttribute("role", "status");
        const message = document.createElement("span"); const retry = document.createElement("button"); retry.className = "image-studio-image-retry"; retry.type = "button"; retry.textContent = "重试";
        retry.addEventListener("click", (event) => { event.stopPropagation(); void retryMobileImage(session); });
        status.append(message, retry); pswp.element.appendChild(status); session.status = status;
        updateMobileViewerFeedback(session);
        pswp.element?.classList.remove("image-studio-controls-visible");
        detailImagePaintRevision++;
        window.ImageStudioBackdrop.pause(els.drawerBody.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)"));
        if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) session.entryAnimation = pswp.element.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 160, easing: "ease-out" });
        syncPageScrollLock();
      });
      pswp.on("openingAnimationEnd", () => pswp.element?.classList.remove("image-studio-controls-visible"));
      pswp.on("destroy", () => {
        const item = session.sequence[pswp.currIndex];
        const shouldSyncDetail = !suppressMobileDetailSync;
        session.active = false;
        cancelMobileWork(session);
        window.ImageStudioBackdrop.dispose(session.backdrop);
        session.retryTimers.forEach((timer) => window.clearTimeout(timer)); session.retryTimers.clear(); session.loads.clear();
        if (mobileViewerSession !== session) return;
        suppressMobileDetailSync = false;
        mobileImageViewer = null; mobileViewerSession = null; mobileImageSequence = []; mobileImageDataSource = []; mobileImageLoads = new Map(); mobileDetailSyncRevision += 1;
        if (shouldSyncDetail) {
          void syncDetailToMobileImage(item).then(() => {
            if (mobileViewerSession) return;
            if (!els.detailDrawer.classList.contains("is-open") || String(state.detailId) !== String(item?.generation_id)) return;
            if (state.detailData && !state.detailAssetsLoaded && state.detailId === item?.generation_id) void loadDetailAssets(state.detailId, state.detailData, state.detailFallbackThumbnail);
            els.detailDrawer.focus();
          }).catch((error) => { if (els.detailDrawer.classList.contains("is-open")) showNotice(errorMessage(error, "生成详情同步失败"), "error"); });
        }
        syncPageScrollLock();
      });
      suppressMobileDetailSync = false;
      mobileImageViewer = pswp;
      mobileViewerSession = session;
      pswp.init();
      return true;
    } finally {
      if (openingRevision === mobileViewerOpenRevision) mobileViewerOpening = false;
    }
  }

  function normalizePreviewItems(items, dataUrl, downloadFilename) {
    const source = Array.isArray(items) && items.length ? items : [{ data_url: dataUrl, download_filename: downloadFilename }];
    return source.map((item) => typeof item === "string" ? { data_url: item } : { ...item, data_url: item?.data_url || item?.thumbnail_data_url || (item?.id ? EMPTY_MOBILE_IMAGE : "") }).filter((item) => item.data_url);
  }

  function renderImagePreview() {
    const items = state.imagePreviewItems;
    const index = Math.max(0, Math.min(items.length - 1, state.imagePreviewIndex));
    const item = items[index];
    if (!item) return;
    state.imagePreviewIndex = index;
    if (state.imagePreviewContext?.type === "detail" && state.imagePreviewContext.generationId === state.detailId && state.detailData?.lightweight) {
      state.detailImageIndex = index; state.detailRequestedImageIndex = index;
      const known = state.detailData.images[index];
      if (known) {
        Object.assign(item, known, { data_url: known.data_url || known.thumbnail_data_url || EMPTY_MOBILE_IMAGE });
        if (!known._originalLoaded) void loadDetailAssets(state.detailId, state.detailData, state.detailFallbackThumbnail);
      }
    }
    const title = items.length > 1 || state.imagePreviewContext?.type === "detail" ? `生成结果 ${index + 1}` : state.imagePreviewTitle;
    const dataUrl = item.data_url;
    const extension = ((dataUrl.match(/^data:image\/([^;]+)/) || [])[1] || "png").replace("jpeg", "jpg");
    const fallbackFilename = `${String(title || "image").replace(/[^\w\u3400-\u9fff-]+/g, "_")}.${extension}`;
    const filename = String(item.download_filename || state.imagePreviewDownloadFilename || fallbackFilename).replace(/[\\/\0]/g, "_");
    els.previewImage.src = dataUrl;
    els.previewImage.alt = title;
    els.imagePreviewTitle.textContent = title;
    els.downloadImageButton.href = dataUrl;
    els.downloadImageButton.download = filename;
    const canCrossPrevious = state.imagePreviewContext?.type === "detail" && hasAdjacentGalleryRecord(-1);
    const canCrossNext = state.imagePreviewContext?.type === "detail" && hasAdjacentGalleryRecord(1);
    const hasCarousel = items.length > 1 || canCrossPrevious || canCrossNext;
    els.imagePreviewPrev.classList.toggle("is-hidden", !hasCarousel);
    els.imagePreviewNext.classList.toggle("is-hidden", !hasCarousel);
    els.imagePreviewPrev.disabled = !hasCarousel || (index <= 0 && !canCrossPrevious);
    els.imagePreviewNext.disabled = !hasCarousel || (index >= items.length - 1 && !canCrossNext);
    els.imagePreviewDots.classList.toggle("is-hidden", items.length <= 1);
    els.imagePreviewDots.innerHTML = items.length > 1 ? items.map((_entry, itemIndex) => `<button class="detail-carousel-dot ${itemIndex === index ? "is-active" : ""}" data-preview-index="${itemIndex}" type="button" aria-label="查看第 ${itemIndex + 1} 张生成结果" aria-current="${itemIndex === index ? "true" : "false"}"></button>`).join("") : "";
    els.imagePreviewDots.querySelectorAll("[data-preview-index]").forEach((dot) => dot.addEventListener("click", () => { state.imagePreviewIndex = Number(dot.dataset.previewIndex); if (state.imagePreviewContext?.type === "detail") state.imagePreviewContext.imageIndex = state.imagePreviewIndex; renderImagePreview(); }));
  }

  async function navigateImagePreview(direction) {
    if (state.imagePreviewNavigating) return;
    const target = state.imagePreviewIndex + direction;
    if (target >= 0 && target < state.imagePreviewItems.length) {
      state.imagePreviewIndex = target;
      if (state.imagePreviewContext?.type === "detail") state.imagePreviewContext.imageIndex = target;
      renderImagePreview();
      return;
    }
    if (state.imagePreviewContext?.type !== "detail") return;
    const originId = state.imagePreviewContext.generationId;
    state.imagePreviewNavigating = true;
    try {
      if (String(state.detailId) === String(originId)) {
        state.detailImageIndex = state.imagePreviewContext.imageIndex;
        state.detailRequestedImageIndex = state.detailImageIndex;
      }
      await navigateDetail(direction);
      if (state.detailId === originId || !state.detailData) return;
      const items = normalizePreviewItems(detailDisplayImages(state.detailData, state.detailFallbackThumbnail), "", "");
      if (!items.length) return;
      state.imagePreviewItems = items;
      state.imagePreviewIndex = direction < 0 ? items.length - 1 : 0;
      state.imagePreviewContext = { type: "detail", generationId: state.detailId, imageIndex: state.imagePreviewIndex };
      renderImagePreview();
    } finally {
      state.imagePreviewNavigating = false;
    }
  }

  function syncDetailFromImagePreview() {
    const context = state.imagePreviewContext;
    if (context?.type !== "detail" || state.detailId !== context.generationId || !state.detailData) return;
    state.detailRequestedImageIndex = context.imageIndex;
    renderDetail(state.detailData, state.detailFallbackThumbnail);
  }

  function bindImagePreviewGestures() {
    els.downloadImageButton.addEventListener("click", async (event) => {
      if (state.imagePreviewContext?.type !== "detail") return;
      const item = state.imagePreviewItems[state.imagePreviewIndex];
      if (!item?.id) return;
      event.preventDefault();
      try { await (await bridge()).download(`gallery/download/${item.id}`, {}, item.download_filename); }
      catch (error) { showNotice(errorMessage(error, "图片下载失败"), "error"); }
    });
    let touchStartX = 0;
    let touchStartY = 0;
    let touchStarted = false;
    els.imagePreviewBody.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 1) { touchStarted = false; return; }
      touchStartX = event.touches[0].clientX;
      touchStartY = event.touches[0].clientY;
      touchStarted = true;
    }, { passive: true });
    els.imagePreviewBody.addEventListener("touchend", (event) => {
      if (!touchStarted) return;
      touchStarted = false;
      const touch = event.changedTouches[0];
      if (!touch) return;
      const deltaX = touch.clientX - touchStartX;
      const deltaY = touch.clientY - touchStartY;
      if (Math.abs(deltaX) < 48 || Math.abs(deltaX) < Math.abs(deltaY) * 1.15) return;
      state.imagePreviewSwipeAt = Date.now();
      event.preventDefault();
      void navigateImagePreview(deltaX < 0 ? 1 : -1);
    }, { passive: false });
    els.imagePreviewBody.addEventListener("touchcancel", () => { touchStarted = false; }, { passive: true });
  }

  function openImagePreview(dataUrl, title, downloadFilename = "", items = null, index = 0, context = null) {
    if (isMobileDetailPreview(context)) {
      void openMobileImageViewer(dataUrl, context).then((opened) => {
        if (!opened) openLegacyImagePreview(dataUrl, title, downloadFilename, items, index, context);
      }).catch((error) => {
        showNotice(errorMessage(error, "全屏图片查看失败"), "error");
        openLegacyImagePreview(dataUrl, title, downloadFilename, items, index, context);
      });
      return;
    }
    openLegacyImagePreview(dataUrl, title, downloadFilename, items, index, context);
  }

  function openLegacyImagePreview(dataUrl, title, downloadFilename = "", items = null, index = 0, context = null) {
    const normalizedItems = normalizePreviewItems(items, dataUrl, downloadFilename);
    if (!normalizedItems.length) return;
    state.imagePreviewItems = normalizedItems;
    state.imagePreviewIndex = Math.max(0, Math.min(normalizedItems.length - 1, Number(index) || 0));
    state.imagePreviewTitle = title || "图片预览";
    state.imagePreviewDownloadFilename = downloadFilename || "";
    state.imagePreviewContext = context ? { ...context, imageIndex: state.imagePreviewIndex } : null;
    renderImagePreview();
    els.imagePreview.classList.remove("is-hidden");
    syncPageScrollLock();
  }

  function closeImagePreview() { syncDetailFromImagePreview(); els.imagePreview.classList.add("is-hidden"); els.previewImage.removeAttribute("src"); els.downloadImageButton.href = "#"; els.downloadImageButton.download = ""; state.imagePreviewItems = []; state.imagePreviewIndex = 0; state.imagePreviewDownloadFilename = ""; state.imagePreviewContext = null; state.imagePreviewNavigating = false; state.imagePreviewSwipeAt = 0; syncPageScrollLock(); }

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
      await bootstrap();
      if (state.detailData?.source === "import") {
        const payload = await apiGet(`gallery/parameters/${id}`, { image_id: state.detailData.images?.[state.detailImageIndex]?.id, format: "studio" });
        await library.resolveParameters(typeof payload.content === "string" ? payload.content : JSON.stringify(payload.content), undefined, { forReproduction: true });
      } else {
        const draft = await apiPost(`gallery/reproduce/${id}`, { image_id: String(state.detailId) === String(id) ? state.detailData?.images?.[state.detailImageIndex]?.id : undefined });
        if (draft.requires_model_selection) {
          const payload = await apiGet(`gallery/parameters/${id}`, { image_id: state.detailData?.images?.[state.detailImageIndex]?.id, format: "studio" });
          await library.resolveParameters(typeof payload.content === "string" ? payload.content : JSON.stringify(payload.content), undefined, { forReproduction: true, references: draft.references || [] });
        } else applyDraft(draft, { forReproduction: true });
      }
    } catch (error) { showNotice(errorMessage(error, "复现参数读取失败"), "error"); }
  }

  function applyDraft(draft, { forReproduction = false } = {}) {
    state.mode = draft.mode === "img2img" ? "img2img" : "text2img"; state.selectedProviderId = draft.provider_id || ""; state.references = draft.references || [];
    state.selectedModelRef = draft.model_ref || (draft.provider_id && draft.model ? `${draft.provider_id}:${draft.model}` : "");
    const rawParameterValues = { ...(draft.parameters || {}) };
    if (Object.prototype.hasOwnProperty.call(draft, "size")) rawParameterValues.size = draft.size;
    if (Object.prototype.hasOwnProperty.call(draft, "count")) rawParameterValues.count = draft.count;
    state.parameterValues = parameterValuesForModel(selectedModel(), rawParameterValues, { forReproduction }); state.parameterCarry = { ...state.parameterValues }; state.negativePromptCarry = draft.negative_prompt ?? ""; state.hasNegativePromptCarry = !!selectedModel()?.supports_negative_prompt;
    els.prompt.value = draft.prompt ?? ""; els.negativePrompt.value = draft.negative_prompt ?? "";
    els.parameters.value = "";
    document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === state.mode)); renderModelChoices();
    els.modelParameters.querySelectorAll("[data-model-parameter]").forEach((input) => { if (state.parameterValues[input.dataset.modelParameter] === null) { input.dataset.nullValue = "true"; input.addEventListener("input", () => { delete input.dataset.nullValue; }, { once: true }); } });
    closeDetail(); switchView("generate"); renderReferences();
    if (draft.notice) setError(els.generationError, draft.notice);
  }

  function populateDefaultModelSelect(select, models, selectedRef) {
    select.innerHTML = `<option value="">未设置</option>${models.map((model) => `<option value="${escape(model.model_ref)}">${escape(model.name)} · ${escape(model.provider_name)}</option>`).join("")}`;
    select.value = models.some((model) => model.model_ref === selectedRef) ? selectedRef : "";
    window.ImageStudioSelect?.refresh(select);
  }

  function refreshSettingsDefaultModels(defaults = null) {
    if (!state.settings) return;
    const models = state.settings.webui.providers.filter((provider) => provider.enabled).flatMap((provider) => (provider.models || []).map((model) => ({ ...model, provider_name: provider.name, model_ref: `${provider.id}:${model.id}` })));
    for (const [select, scope, mode, supported] of [
      [els.settingPageDefaultTextModel, "page", "text2img", "supports_text2img"],
      [els.settingPageDefaultImageModel, "page", "img2img", "supports_img2img"],
      [els.settingToolDefaultTextModel, "tool", "text2img", "supports_text2img"],
      [els.settingToolDefaultImageModel, "tool", "img2img", "supports_img2img"],
    ]) {
      const available = models.filter((model) => model[supported] && (scope !== "tool" || model.tool?.enabled !== false));
      populateDefaultModelSelect(select, available, defaults ? defaults[scope]?.[`${mode}_model_ref`] || "" : select.value);
    }
  }

  function syncAgentImageSettings() { els.agentPreviewMaxEdge.disabled = false; els.agentPreviewQuality.disabled = false; }

  function renderStorageHealth(report) {
    const stats = report?.stats || {}; const errors = Array.isArray(report?.errors) ? report.errors : [];
    const labels = { healthy: "正常", warning: "需关注", error: "异常", never: "未检查" };
    els.storageHealthStatus.textContent = report?.running ? "检查中" : (labels[report?.status] || "未知");
    els.storageHealthStatus.dataset.status = report?.status || "never";
    els.storageHealthCheckedAt.textContent = report?.checked_at ? formatDate(report.checked_at) : "尚未执行";
    els.storageHealthDuration.textContent = report?.duration_ms >= 0 ? `${Number(report.duration_ms)} ms` : "-";
    els.storageHealthAssets.textContent = `${Number(stats.assets || 0)} / ${Number(stats.thumbnails || 0)}`;
    els.storageHealthLeases.textContent = String(Number(stats.active_leases || 0)); els.storageHealthGenerations.textContent = String(Number(stats.generations || 0)); els.storageHealthSize.textContent = formatBytes(stats.size_bytes || 0);
    els.storageHealthErrors.textContent = errors.length ? errors.join("；") : "暂无异常。";
    if (report?.retention) storageRetention = report.retention;
    renderStorageQuotas();
  }

  function renderStorageQuotas() {
    const retention = storageRetention;
    for (const [id, used, limit, exempt, unit] of [
      ["storageQuotaRecords", retention?.record_count, state.settings ? Number(els.historyRecords.value) : retention?.limit_records, retention?.exempt_record_count, "records"],
      ["storageQuotaBytes", retention?.size_bytes, state.settings ? Number(els.historyMegabytes.value) * 1024 * 1024 : retention?.limit_bytes, retention?.exempt_size_bytes, "bytes"],
    ]) {
      const container = $(id); container.classList.toggle("is-hidden", !retention || !(limit > 0));
      if (!retention || !(limit > 0)) continue;
      const current = Math.max(0, Number(used) || 0); const ratio = current / limit;
      const format = (value) => unit === "records" ? `${Number(value || 0).toLocaleString()} 条` : formatBytes(value);
      container.dataset.status = ratio > 1 ? "over" : ratio >= .9 ? "warning" : "normal";
      $(id + "Label").textContent = `${format(current)} / ${format(limit)} · ${(ratio * 100).toFixed(1)}%`;
      const progress = $(id + "Progress"); progress.max = limit; progress.value = Math.min(current, limit);
      progress.setAttribute("aria-valuetext", `${format(current)}，限额 ${format(limit)}${ratio > 1 ? "，已超出限额" : ""}`);
      $(id + "Exempt").textContent = `限额外（豁免）：${format(exempt)}`;
    }
  }

  async function loadStorageHealth() { try { renderStorageHealth(await apiGet("storage/health")); } catch (error) { els.storageHealthStatus.textContent = "读取失败"; els.storageHealthErrors.textContent = errorMessage(error, "存储状态读取失败"); } }

  async function runStorageMaintenance(deep) {
    if (deep && !await confirmAction("深度检查会重新计算全部原图哈希，历史较多时可能耗时较长。继续执行？")) return;
    els.runMaintenanceButton.disabled = true; els.runDeepMaintenanceButton.disabled = true; els.storageHealthStatus.textContent = "检查中";
    try { const report = await apiPost("storage/maintenance", { deep: !!deep }); renderStorageHealth(report); await loadStorageHealth(); showNotice(deep ? "存储深度检查已完成。" : "存储检查已完成。", report.status === "error" ? "error" : "success"); }
    catch (error) { showNotice(errorMessage(error, "存储检查失败"), "error"); await loadStorageHealth(); }
    finally { els.runMaintenanceButton.disabled = false; els.runDeepMaintenanceButton.disabled = false; }
  }

  async function loadSettings(force = false, suppliedPayload = null) {
    if (state.settings && !force) return true;
    if (settingsLoadPromise) return settingsLoadPromise;
    els.addProviderButton.disabled = true; els.addModelButton.disabled = true; els.saveSettingsButton.disabled = true;
    setError(els.settingsError, "正在读取设置…");
    settingsLoadPromise = (async () => {
      try {
        const payload = suppliedPayload || await apiGet("settings/get");
        if (!payload?.base || !payload?.webui || !Array.isArray(payload.webui.providers)) throw new Error("设置接口返回的数据格式无效");
        normalizeSettingsModelDefaults(payload);
        state.settings = payload;
        els.settingTool.checked = !!payload.base.enable_llm_tool;
        const llmPolicy = payload.webui.llm_policy || {}; const assetPolicy = payload.webui.asset_policy || {}; els.agentImageReturnMode.value = ["asset", "preview", "original"].includes(llmPolicy.image_return_mode) ? llmPolicy.image_return_mode : "preview"; els.agentPreviewMaxEdge.value = Number(assetPolicy.preview_max_edge || 768); els.agentPreviewQuality.value = Number(assetPolicy.preview_quality || 80); els.agentAssetRetentionHours.value = Number(assetPolicy.lease_hours || 24); syncAgentImageSettings();
        const history = payload.webui.history; els.historyEnabled.checked = !!history.enabled; els.retainReferences.checked = !!history.retain_reference_images; els.recordInvocationIdentity.checked = !!history.record_invocation_identity; els.historyRecords.value = history.max_records; els.historyMegabytes.value = history.max_megabytes;
        refreshSettingsDefaultModels(payload.webui.generation_defaults || {});
        if (!payload.webui.providers.some((item) => item.id === state.selectedSettingsProviderId)) state.selectedSettingsProviderId = payload.webui.providers[0]?.id || "";
        state.selectedSettingsModelId = "";
        renderSettingsProviders();
        window.ImageStudioSelect?.refresh($("settingsView"));
        settingsBaseline = settingsFingerprint(settingsDraft()); updateSettingsDirty();
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
  const BATCH_PRESET = {
    count: { type: "integer", label: "生图张数", description: "本次生成的总图片数量，插件根据模型单次请求图片上限自动分批。取值范围：[1, 16]。", default: 1, min: 1, max: 16, step: 1, request_key: "count", refill_from_history: false },
  };
  const MODEL_PRESETS = {
    openai_images: { size: { type: "select", label: "尺寸", default: "1024x1024", choices: ["1024x1024", "1536x1024", "1024x1536"], request_key: "size" }, count: { type: "number", label: "数量", default: 1, min: 1, max: 4, step: 1, request_key: "count" }, quality: { type: "select", label: "质量", default: "auto", choices: ["auto", "low", "medium", "high"], request_key: "quality" }, background: { type: "select", label: "背景", default: "auto", choices: ["auto", "transparent", "opaque"], request_key: "background" }, output_format: { type: "select", label: "输出格式", default: "png", choices: ["png", "jpeg", "webp"], request_key: "output_format" } },
    gemini: { aspect_ratio: { type: "select", label: "画面比例", default: "1:1", choices: ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"], request_key: "aspect_ratio" }, image_size: { type: "select", label: "图片尺寸", default: "1K", choices: ["1K", "2K", "4K"], request_key: "image_size" } },
    nai_direct: {
      ...BATCH_PRESET,
      style: { type: "preset", label: "绘画风格", description: "选择预设绘画风格；自定义会清空画师串。", default: "custom", target: "artist", ui_only: true, choices: [{ value: "vertical", label: "韩漫小清新风", fill: NAI_ARTIST_PRESETS.vertical }, { value: "comicDoujin", label: "漫画同人风", fill: NAI_ARTIST_PRESETS.comicDoujin }, { value: "r18", label: "2.5D唯美风", fill: NAI_ARTIST_PRESETS.r18 }, { value: "lolita25d", label: "2.5D唯美风（萝）", fill: NAI_ARTIST_PRESETS.lolita25d }, { value: "anime", label: "本子里番风", fill: NAI_ARTIST_PRESETS.anime }, { value: "galgame", label: "GalGame风", fill: NAI_ARTIST_PRESETS.galgame }, { value: "custom", label: "自定义", fill: "" }] }, artist: { type: "textarea", label: "画师串", description: "追加到提示词前方的画师与质量标签串。", default: "", request_key: "artist" }, size: { type: "select", label: "尺寸", description: "nai.sta1n.cn 使用的中文画幅与分辨率名称。", default: "竖图", choices: ["竖图", "横图", "方图", "2K竖图", "2K横图", "2K方图", "4K竖图", "4K横图", "4K方图"], request_key: "size" }, sampler: { type: "select", label: "采样器", description: "控制从噪声生成画面的采样方式。", default: "k_euler_ancestral", choices: ["k_dpmpp_2m_sde", "k_dpmpp_2m", "k_dpmpp_sde", "k_dpmpp_2s_ancestral", "k_euler_ancestral", "k_euler", "ddim"], request_key: "sampler" }, steps: { type: "number", label: "采样步数", description: "采样迭代次数；当前第三方接口路由限制为 1–28。", default: 24, min: 1, max: 28, step: 1, request_key: "steps" }, scale: { type: "number", label: "提示词引导强度", description: "数值越高越强调遵循提示词，过高可能产生不自然效果。", default: 6, min: 1, max: 20, step: 0.1, request_key: "scale" }, cfg: { type: "number", label: "CFG Rescale", description: "缓解高提示词引导造成的颜色过饱和；上游映射为 cfg_rescale。", default: 0.3, min: 0, max: 1, step: 0.05, request_key: "cfg" }, noise_schedule: { type: "select", label: "噪声调度", description: "控制采样过程中的噪声变化曲线。", default: "karras", choices: ["karras", "exponential", "polyexponential", "native"], request_key: "noise_schedule" } },
    custom_json: { size: { type: "text", label: "尺寸（可选）", default: "1024x1024", request_key: "size" }, count: { type: "number", label: "数量", default: 1, min: 1, max: 4, step: 1, request_key: "count" } },
  };
  function providerDefaults(kind) { return { ...(PROVIDER_DEFAULTS[kind] || PROVIDER_DEFAULTS.custom_json) }; }
  function modelPreset(kind) {
    const preset = JSON.parse(JSON.stringify({ ...BATCH_PRESET, ...(MODEL_PRESETS[kind] || MODEL_PRESETS.custom_json) }));
    Object.entries(preset).forEach(([name, descriptor]) => { if (modelParameterMatches(name, descriptor, ["count", "n"])) descriptor.refill_from_history = false; });
    if (kind === "nai_direct") preset.style.record_in_history = false;
    return preset;
  }
  function modelParameterMatches(name, descriptor, keys) { return keys.includes(name) || keys.includes(descriptor?.request_key); }
  const MODEL_SCHEDULING_FIELDS = ["batch_mode", "concurrency", "native_count_supported", "native_batch_size", "native_batch_size_source", "max_concurrent_requests"];
  function effectiveModelParameters(model) { return Object.entries(model?.parameters || {}).filter(([name, descriptor]) => !modelParameterMatches(name, descriptor, MODEL_SCHEDULING_FIELDS)); }
  function ensureBatchConfig(model, provider) {
    const nai = provider.kind === "nai_direct";
    model.native_batch_size = nai ? 1 : Number(model.native_batch_size ?? 1);
    model.native_batch_size_source = nai ? "fixed" : model.native_batch_size_source || "default";
    model.max_concurrent_requests = Number(model.max_concurrent_requests ?? 8);
    model.parameters = model.parameters || {};
    if (!Object.entries(model.parameters).some(([name, descriptor]) => modelParameterMatches(name, descriptor, ["count", "n"]))) model.parameters.count = JSON.parse(JSON.stringify(BATCH_PRESET.count));
  }
  function currentSettingsProvider() { return state.settings?.webui.providers.find((item) => item.id === state.selectedSettingsProviderId) || null; }
  function renderSettingsProviders() {
    const providers = state.settings?.webui.providers || []; els.settingsProviderList.innerHTML = providers.length ? providers.map((item) => `<button class="provider-row ${item.id === state.selectedSettingsProviderId ? "is-active" : ""}" type="button" data-settings-provider="${escape(item.id)}"><strong>${escape(item.name || item.id)}</strong><span>${item.enabled ? "启用" : "停用"}</span></button>`).join("") : '<div class="provider-empty">尚未添加生图服务商</div>';
    els.settingsProviderList.querySelectorAll("[data-settings-provider]").forEach((button) => button.addEventListener("click", () => { state.selectedSettingsProviderId = button.dataset.settingsProvider; state.selectedSettingsModelId = ""; renderSettingsProviders(); }));
    renderProviderEditor();
    renderModelEditor();
    updateSettingsDirty();
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
    window.ImageStudioSelect?.refresh(els.providerForm);
  }
  function field(key, label, value, type = "text") { return `<div class="field"><label>${label}</label><input data-provider-field="${key}" type="${type}" value="${escape(value)}" /></div>`; }
  function textAreaField(key, label, value) { return `<div class="field field-wide"><label>${label}</label><textarea data-provider-field="${key}" rows="3">${escape(value)}</textarea></div>`; }
  function selectField(key, label, value, options) { return `<div class="field"><label>${label}</label><select data-provider-field="${key}">${options.map(([id, name]) => `<option value="${id}" ${id === value ? "selected" : ""}>${name}</option>`).join("")}</select></div>`; }
  function toggleField(key, label, value) { return `<div class="toggle-row"><label>${label}</label><label class="toggle-control"><input data-provider-field="${key}" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`; }
  function updateProviderField(input) { const provider = currentSettingsProvider(); if (!provider) return; const key = input.dataset.providerField; const value = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value; if (key === "kind" && value !== provider.kind) { Object.assign(provider, providerDefaults(value)); provider.kind = value; state.selectedSettingsModelId = ""; renderProviderEditor(); renderModelEditor(); return; } provider[key] = value; if (key === "id") { state.selectedSettingsProviderId = input.value; const activeRow = els.settingsProviderList.querySelector(".provider-row.is-active"); if (activeRow) { activeRow.dataset.settingsProvider = input.value; if (!provider.name) activeRow.querySelector("strong").textContent = input.value; } } refreshSettingsDefaultModels(); }
  async function discoverProviderModels(provider) { const button = $("discoverModelsButton"); if (button) { button.disabled = true; button.textContent = "获取中…"; } try { const payload = await apiPost("provider/models", { provider }); provider.discovered_models = payload.models || []; for (const model of provider.models || []) { const discovered = provider.discovered_models.find((item) => item.id === model.id); if (discovered && model.native_batch_size_source !== "manual") { model.native_batch_size = Number(discovered.native_batch_size) || 1; model.native_batch_size_source = discovered.native_batch_size_source || "default"; } } renderModelEditor(); updateSettingsDirty(); showNotice(`已获取 ${provider.discovered_models.length} 个模型，可在新增模型时选择。`, "success"); } catch (error) { showNotice(errorMessage(error, "获取模型失败"), "error"); } finally { if (button) { button.disabled = false; button.textContent = "获取模型"; } } }
  function renderNewModelChoices(provider = currentSettingsProvider()) { const models = provider?.kind === "nai_direct" ? NAI_MODELS : provider?.discovered_models || []; els.newModelChoices.innerHTML = models.map((item) => `<option value="${escape(item.id)}">${escape(item.name || item.id)}${item.capability_source === "unknown" ? " · 能力未知" : ""}</option>`).join(""); els.newModelChoice.value = ""; els.newModelChoice.placeholder = provider?.kind === "nai_direct" ? "选择 NAI 模型或手动输入 ID" : "选择或输入模型 ID"; }

  function currentSettingsModel() { const provider = currentSettingsProvider(); return provider?.models?.find((item) => item.id === state.selectedSettingsModelId) || null; }
  function renderModelEditor() {
    const provider = currentSettingsProvider();
    refreshSettingsDefaultModels();
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
    window.ImageStudioSelect?.refresh(els.modelForm);
  }
  function renderModelConfiguration(provider, model) {
    const schemaText = JSON.stringify(model.parameters || modelPreset(provider.kind), null, 2);
    const discoveredIds = (provider.discovered_models || []).map((item) => item.id); const modelChoices = provider.kind === "nai_direct" ? ["nai-diffusion-4-5-full", "nai-diffusion-5-full"] : discoveredIds;
    const modelIdField = modelChoices.length ? modelSelectField("id", "模型 ID", model.id, modelChoices, true) : modelField("id", "模型 ID", model.id);
    const negativeDefaultField = model.supports_negative_prompt ? modelTextAreaField("negative_prompt_default", "默认反向提示词", model.negative_prompt_default || "") : "";
    const testDisabled = !model.supports_text2img;
    const defaults = `<section class="schema-preview"><h4>参数默认值</h4>${effectiveModelParameters(model).map(([name, descriptor]) => renderSchemaDefault(name, descriptor)).join("") || '<span class="field-hint">当前 schema 没有参数。</span>'}</section>`;
    const batchFields = `<div class="field"><label title="单次接口请求最多生成的图片张数；总张数超出时自动分批。">单次请求图片上限</label><input data-model-field="native_batch_size" type="number" min="1" step="1" value="${escape(model.native_batch_size)}"${provider.kind === "nai_direct" ? " disabled" : ""} /></div><div class="field"><label title="该模型在所有任务中共享的最大并发请求数，仍受服务商最大并发限制。取值范围：[1, 16]。">模型最大并发请求数</label><input data-model-field="max_concurrent_requests" type="number" min="1" max="16" step="1" value="${escape(model.max_concurrent_requests)}" /></div>`;
    const raw = `<details class="schema-raw"><summary>高级：参数 Schema</summary><textarea id="modelParametersSchema" data-model-field="parameters" rows="14" spellcheck="false">${escape(schemaText)}</textarea><span class="field-hint">每个字段支持 type、label、description、default、request_key、min、max、step、choices、webui_visible、record_in_history、refill_from_history。</span></details>`;
    const capabilityEditable = ["unknown", "manual"].includes(model.capability_source);
    const capabilityHint = model.supports_img2img ? `<div class="field"><label>参考图能力上限</label><input data-model-field="max_reference_images" type="number" min="1" max="8" step="1" value="${configuredReferenceLimit(model.max_reference_images)}"${capabilityEditable ? "" : " disabled"} /><span class="field-hint">${capabilityEditable ? "无法获取时可手动填写，取值范围：1–8 张。" : `来源：${escape(model.capability_source)}，已获取的能力不可在此覆盖。`}</span></div>` : "";
    return `<h3>${escape(model.name || model.id)}</h3>${modelIdField}${modelField("name", "显示名称", model.name)}${batchFields}${modelToggle("supports_text2img", "支持文生图", model.supports_text2img)}${modelToggle("supports_img2img", "支持图生图", model.supports_img2img, provider.kind === "nai_direct")}${modelToggle("supports_negative_prompt", "支持专用反向提示词", model.supports_negative_prompt, provider.kind === "gemini")}${capabilityHint}${negativeDefaultField}${defaults}${raw}<div class="provider-editor-actions"><button class="danger-button" id="removeModelButton" type="button">删除模型</button><button class="quiet-button" id="testModelButton" type="button"${testDisabled ? ' disabled title="仅支持图生图的模型需要参考图，暂不能在此测试"' : ""}>测试模型</button></div>`;
  }
  function renderSchemaDefault(name, descriptor) {
    const type = String(descriptor.type || "text").toLowerCase();
    const title = escape(descriptor.description || descriptor.label || name);
    const value = descriptor.default ?? "";
    const label = `<div class="field-label-row"><label title="${title}">${escape(name)}</label>${library.schemaPolicyButton(name)}</div>`;
    if ((type === "select" || type === "preset") && Array.isArray(descriptor.choices)) return `<div class="field">${label}<select data-schema-default="${escape(name)}">${descriptor.choices.map((choice) => { const item = typeof choice === "object" ? choice : { value: choice, label: choice }; return `<option value="${escape(item.value)}" ${String(item.value) === String(value) ? "selected" : ""}>${escape(item.label || item.value)}</option>`; }).join("")}</select></div>`;
    if (type === "boolean" || type === "bool") return `<div class="field">${label}<label class="toggle-control"><input data-schema-default="${escape(name)}" aria-label="${escape(name)} 默认值" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`;
    const inputType = ["number", "int", "integer", "float"].includes(type) ? "number" : "text";
    return `<div class="field">${label}<input data-schema-default="${escape(name)}" type="${inputType}" value="${escape(value)}"${descriptor.min !== undefined ? ` min="${escape(descriptor.min)}"` : ""}${descriptor.max !== undefined ? ` max="${escape(descriptor.max)}"` : ""}${descriptor.step !== undefined ? ` step="${escape(descriptor.step)}"` : ""} /></div>`;
  }
  function toolModelParameters(model) {
    const entries = effectiveModelParameters(model).filter(([name, descriptor]) => name !== "negative_prompt" && (!descriptor.ui_only || String(descriptor.type).toLowerCase() === "preset"));
    if (model.supports_negative_prompt) entries.unshift(["negative_prompt", { type: "textarea", label: "反向提示词", description: "专用反向提示词，只填写不希望出现在画面中的内容；省略时使用此处的默认值。", default: model.negative_prompt_default ?? "" }]);
    return entries;
  }
  function toolParameterDescriptor(model, name) { return toolModelParameters(model).find(([key]) => key === name)?.[1] || {}; }
  function renderToolConfiguration(model) { const tool = model.tool; const configuredLimit = configuredReferenceLimit(model.max_reference_images); const refLimit = model.supports_img2img ? `<div class="field"><label>LLM 最大参考图数量</label><input data-tool-field="max_reference_images" type="number" min="1" max="${configuredLimit}" step="1" value="${configuredReferenceLimit(tool.max_reference_images, configuredLimit)}" /><span class="field-hint">不能超过模型能力上限 ${configuredLimit}</span></div>` : ""; const rows = toolModelParameters(model).map(([name, descriptor]) => { const policy = tool.parameters?.[name] || {}; return `<div class="tool-parameter-row"><strong title="${escape(policy.description || descriptor.description || descriptor.label || name)}">${escape(name)}</strong><span>${policy.exposed === false ? "未暴露" : "已暴露"}</span><button class="quiet-button" data-edit-tool-parameter="${escape(name)}" type="button">编辑</button></div>`; }).join(""); return `<h3>${escape(model.name || model.id)}</h3>${modelToggle("tool_enabled", "允许 LLM 调用此模型", tool.enabled !== false)}${modelTextAreaField("tool_selection_description", "什么时候使用", tool.selection_description || "")}${modelSelectField("tool_prompt_profile", "提示词类型", tool.prompt_profile || "natural_language", ["natural_language", "nai_tags", "custom"])}${modelTextAreaField("tool_prompt_instructions", "提示词编写要求", tool.prompt_instructions || "")}${refLimit}<div class="tool-parameter-list"><span class="field-hint">LLM 可用参数</span>${rows || '<span class="field-hint">当前模型没有可暴露参数。</span>'}</div>`; }
  function bindModelConfiguration(provider, model) {
    els.modelForm.querySelectorAll("[data-model-field]").forEach((input) => { input.addEventListener("input", () => updateModelField(input)); input.addEventListener("change", () => updateModelField(input, true)); });
    els.modelForm.querySelectorAll("[data-schema-default]").forEach((input) => input.addEventListener("change", () => { const descriptor = model.parameters[input.dataset.schemaDefault]; descriptor.default = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value; const raw = $("modelParametersSchema"); if (raw) raw.value = JSON.stringify(model.parameters, null, 2); }));
    els.modelForm.querySelectorAll("[data-tool-field]").forEach((input) => {
      const update = (commit) => { const key = input.dataset.toolField; model.tool[key] = key === "max_reference_images" ? configuredReferenceLimit(input.value, configuredReferenceLimit(model.max_reference_images)) : input.type === "number" ? Number(input.value) : input.value; if (commit && key === "max_reference_images") input.value = model.tool[key]; refreshSettingsDefaultModels(); };
      input.addEventListener("input", () => update(false)); input.addEventListener("change", () => update(true));
    });
    els.modelForm.querySelectorAll("[data-edit-tool-parameter]").forEach((button) => button.addEventListener("click", () => openToolParameterDialog(button.dataset.editToolParameter)));
    els.modelForm.querySelectorAll("[data-edit-schema-policy]").forEach((button) => button.addEventListener("click", async () => {
      const name = button.dataset.editSchemaPolicy;
      const policy = await library.editParameterPolicy(name, model.parameters[name]);
      if (!policy) return;
      Object.assign(model.parameters[name], policy);
      const raw = $("modelParametersSchema"); if (raw) raw.value = JSON.stringify(model.parameters, null, 2);
      updateSettingsDirty();
    }));
    $("removeModelButton")?.addEventListener("click", async () => { if (!await confirmAction("删除此模型？历史记录不会删除。")) return; provider.models = provider.models.filter((item) => item.id !== model.id); state.selectedSettingsModelId = provider.models[0]?.id || ""; renderModelEditor(); showNotice("已从设置草稿中删除模型，保存全部设置后生效。", "success"); });
    $("testModelButton")?.addEventListener("click", () => void testModel(provider, model));
  }
  function modelField(key, label, value, type = "text") { return `<div class="field"><label>${label}</label><input data-model-field="${key}" type="${type}" value="${escape(value)}" /></div>`; }
  function modelTextAreaField(key, label, value) { return `<div class="field field-wide"><label>${label}</label><textarea data-model-field="${key}" rows="4">${escape(value)}</textarea></div>`; }
  function modelSelectField(key, label, value, choices, editable = false) { const values = choices.includes(value) ? choices : [value, ...choices]; return `<div class="field"><label>${label}</label><select data-model-field="${key}">${values.map((item) => `<option value="${escape(item)}" ${item === value ? "selected" : ""}>${escape(item)}</option>`).join("")}${editable ? '<option value="__manual__">手动输入…</option>' : ""}</select></div>`; }
  function modelToggle(key, label, value, disabled = false) { return `<div class="toggle-row"><label>${label}</label><label class="toggle-control"><input data-model-field="${key}" type="checkbox" ${value ? "checked" : ""}${disabled ? " disabled" : ""} /><span aria-hidden="true"></span></label></div>`; }
  function updateModelField(input, commit = false) {
    const model = currentSettingsModel(); if (!model) return;
    const key = input.dataset.modelField;
    if (key === "parameters") { try { model.parameters = input.value.trim() ? JSON.parse(input.value) : {}; input.setCustomValidity(""); } catch { input.setCustomValidity("参数 schema 必须是合法 JSON"); } return; }
    if (key.startsWith("tool_")) { const toolKey = key.slice(5); model.tool[toolKey] = input.type === "checkbox" ? input.checked : input.value; if (toolKey === "enabled") refreshSettingsDefaultModels(); return; }
    if (key === "id" && input.value === "__manual__") { const manual = window.prompt("输入模型 ID", model.id); if (!manual?.trim()) { input.value = model.id; return; } input.value = manual.trim(); }
    model[key] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
    if (key === "native_batch_size") {
      model.native_batch_size_source = currentSettingsProvider().kind === "nai_direct" ? "fixed" : "manual";
    } else if (key === "supports_img2img") {
      renderModelEditor();
    } else if (key === "max_reference_images") {
      model.max_reference_images = configuredReferenceLimit(input.value); if (commit) input.value = model.max_reference_images;
      model.capability_source = "manual";
      if (commit) model.tool.max_reference_images = configuredReferenceLimit(model.tool.max_reference_images, model.max_reference_images);
    } else if (key === "supports_text2img" || key === "supports_negative_prompt") {
      if (key === "supports_negative_prompt" && !model.supports_negative_prompt) model.tool.negative_prompt_exposed = false;
      renderModelEditor();
    } else if (key === "id") {
      state.selectedSettingsModelId = input.value;
      const activeRow = els.settingsModelList.querySelector(".provider-row.is-active");
      if (activeRow) { activeRow.dataset.settingsModel = input.value; if (!model.name) activeRow.querySelector("strong").textContent = input.value; }
    }
    refreshSettingsDefaultModels();
  }
  function defaultToolParameterDescription(name, descriptor) {
    const base = String(descriptor.description || descriptor.label || name);
    const numeric = ["number", "int", "integer", "float"].includes(String(descriptor.type || "").toLowerCase());
    if (!numeric) return base;
    const hasMin = descriptor.min !== undefined && descriptor.min !== null; const hasMax = descriptor.max !== undefined && descriptor.max !== null;
    if (!hasMin && !hasMax) return base;
    const separator = /[。！？.!?]$/.test(base) ? "" : "。";
    const range = hasMin && hasMax ? `取值范围：[${descriptor.min}, ${descriptor.max}]。` : hasMin ? `取值范围：不小于 ${descriptor.min}。` : `取值范围：不大于 ${descriptor.max}。`;
    if (base.endsWith(range)) return base;
    return `${base}${separator}${range}`;
  }
  function ensureToolConfig(model, provider) {
    ensureBatchConfig(model, provider);
    const nai = provider.kind === "nai_direct";
    model.max_reference_images = configuredReferenceLimit(model.max_reference_images);
    if (nai) model.supports_img2img = false;
    const defaults = { enabled: true, selection_description: nai ? "仅在用户明确要求 NAI 或 NovelAI 风格标签生图时使用。" : "适合一般自然语言生图需求。", prompt_profile: nai ? "nai_tags" : "natural_language", prompt_instructions: nai ? "使用英文逗号分隔标签。必须完整描述主体数量、全身或半身范围、姿态、镜头距离、视角、背景、光照和画面边界，避免残图；不得改变用户明确指定的主体、数量、动作和服装。" : "使用清晰、连贯的自然语言描述，不要使用英文逗号分隔的 NAI tag 串。", negative_prompt_exposed: !!model.supports_negative_prompt, max_reference_images: model.max_reference_images, parameters: {} };
    model.tool = { ...defaults, ...(model.tool || {}) }; if (!model.supports_negative_prompt) model.tool.negative_prompt_exposed = false; model.tool.parameters = model.tool.parameters || {};
    model.tool.max_reference_images = configuredReferenceLimit(model.tool.max_reference_images, model.max_reference_images);
    toolModelParameters(model).forEach(([name, descriptor]) => {
      const current = model.tool.parameters[name] || {}; const legacyDescription = descriptor.description || descriptor.label || name;
      const description = !current.description || current.description === legacyDescription ? defaultToolParameterDescription(name, descriptor) : current.description;
      const exposed = name === "negative_prompt" ? model.tool.negative_prompt_exposed !== false : true;
      model.tool.parameters[name] = { exposed, ...current, description };
    });
    if (model.supports_negative_prompt) model.tool.negative_prompt_exposed = model.tool.parameters.negative_prompt.exposed !== false;
  }
  function toolDefaultChoices(descriptor) { if (!Array.isArray(descriptor?.choices)) return []; return descriptor.choices.flatMap((choice) => { if (choice && typeof choice === "object") { if (!Object.prototype.hasOwnProperty.call(choice, "value")) return []; return [{ value: choice.value, label: choice.label ?? choice.value }]; } return [{ value: choice, label: choice }]; }); }
  function sameToolDefault(left, right) { return JSON.stringify(left) === JSON.stringify(right); }
  function toolDefaultLabel(value) { if (value === undefined) return "未设置"; if (value === "") return "空字符串"; if (value && typeof value === "object") return JSON.stringify(value); return String(value); }
  function openToolParameterDialog(name) {
    const model = currentSettingsModel(); if (!model) return;
    ensureToolConfig(model, currentSettingsProvider());
    const descriptor = toolParameterDescriptor(model, name); const policy = model.tool.parameters[name] || {};
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
    els.parameterDialog.classList.remove("is-hidden"); els.scrim.classList.remove("is-hidden"); syncPageScrollLock();
    window.ImageStudioSelect?.refresh(els.parameterDialog);
  }
  function closeToolParameterDialog() { state.editingToolParameter = ""; state.editingToolDefaultChoices = []; els.parameterDialog.classList.add("is-hidden"); if (!els.detailDrawer.classList.contains("is-open")) els.scrim.classList.add("is-hidden"); syncPageScrollLock(); }
  function applyToolParameterDialog() {
    const model = currentSettingsModel(); const name = state.editingToolParameter; if (!model || !name) return;
    let choiceDescriptions = {}; try { choiceDescriptions = els.toolParameterChoices.value.trim() ? JSON.parse(els.toolParameterChoices.value) : {}; } catch { showNotice("选项说明必须是合法 JSON。", "error"); return; }
    if (!choiceDescriptions || typeof choiceDescriptions !== "object" || Array.isArray(choiceDescriptions)) { showNotice("选项说明必须是 JSON 对象。", "error"); return; }
    const descriptor = toolParameterDescriptor(model, name); const policy = { exposed: els.toolParameterExposed.checked, description: els.toolParameterDescription.value };
    if (Object.keys(choiceDescriptions).length) policy.choice_descriptions = choiceDescriptions;
    if (state.editingToolDefaultChoices.length) {
      if (els.toolParameterDefaultChoice.value !== MODEL_DEFAULT_CHOICE) {
        const selected = state.editingToolDefaultChoices[Number(els.toolParameterDefaultChoice.value)];
        if (selected) policy.default_override = selected.value;
      }
    } else if (els.toolParameterDefault.value !== "") {
      policy.default_override = ["number", "int", "integer", "float"].includes(String(descriptor.type).toLowerCase()) ? Number(els.toolParameterDefault.value) : els.toolParameterDefault.value;
    }
    model.tool.parameters[name] = policy;
    if (name === "negative_prompt") model.tool.negative_prompt_exposed = policy.exposed;
    closeToolParameterDialog(); renderModelEditor(); updateSettingsDirty();
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
    const chosen = discovered || (naiChoice ? { ...naiChoice, supports_text2img: true, supports_img2img: false, supports_negative_prompt: true, max_reference_images: 1, capability_source: "builtin" } : null);
    const capabilityKnown = !!chosen?.capability_source && chosen.capability_source !== "unknown";
    const maxRefs = capabilityKnown ? configuredReferenceLimit(chosen.max_reference_images) : 1;
    provider.models.push({ id: requestedId, name: chosen?.name || requestedId, native_batch_size: provider.kind === "nai_direct" ? 1 : Number(chosen?.native_batch_size) || 1, native_batch_size_source: provider.kind === "nai_direct" ? "fixed" : chosen?.native_batch_size_source || "default", max_concurrent_requests: 8, supports_text2img: chosen ? !!chosen.supports_text2img : true, supports_img2img: provider.kind !== "nai_direct" && capabilityKnown ? !!chosen.supports_img2img : false, supports_negative_prompt: chosen ? !!chosen.supports_negative_prompt : provider.kind === "nai_direct", negative_prompt_default: provider.kind === "nai_direct" ? NAI_DEFAULT_NEGATIVE : "", max_reference_images: maxRefs, capability_source: chosen?.capability_source || "manual", parameters: modelPreset(provider.kind), tool: { enabled: true, max_reference_images: maxRefs } });
    state.selectedSettingsModelId = requestedId; renderModelEditor(); showNotice("已新增模型，请填写能力和参数 schema。", "success");
  }
  async function saveSettings() {
    if (settingsSaving) return;
    if (!state.settings && !await loadSettings()) return;
    const invalid = $("settingsView").querySelector("input:invalid, textarea:invalid, select:invalid");
    if (invalid) { invalid.reportValidity(); setError(els.settingsError, "请先修正无效的设置项。"); return; }
    settingsSaving = true; setError(els.settingsError, "正在保存设置…"); els.saveSettingsButton.disabled = true; library.setCommandLabel("saveSettingsButton", "保存中…");
    const draft = settingsDraft(); const submitted = settingsFingerprint(draft); const webui = draft.studio;
    try {
      await apiPost("settings/save", { settings_revision: webui.revision ?? webui.ui?.settings_revision, ...draft });
      const server = await apiGet("settings/get");
      normalizeSettingsModelDefaults(server);
      await bootstrap();
      if (settingsFingerprint(settingsDraft()) === submitted) {
        await loadSettings(true, server);
      } else {
        state.settings.webui.revision = server.webui.revision;
        settingsBaseline = settingsFingerprint({ base: server.base, studio: server.webui });
      }
      setError(els.settingsError, ""); showNotice("设置已保存并生效。", "success");
      await loadStorageHealth();
    } catch (error) {
      const message = errorMessage(error, "设置保存失败"); setError(els.settingsError, message); showNotice(message, "error");
    } finally { settingsSaving = false; els.saveSettingsButton.disabled = false; library.setCommandLabel("saveSettingsButton", "保存全部设置"); updateSettingsDirty(); }
  }

  function settingsDraft() {
    if (!state.settings) return null;
    const webui = JSON.parse(JSON.stringify(state.settings.webui));
    webui.history = { ...(webui.history || {}), enabled: els.historyEnabled.checked, retain_reference_images: els.retainReferences.checked, record_invocation_identity: els.recordInvocationIdentity.checked, max_records: Number(els.historyRecords.value), max_megabytes: Number(els.historyMegabytes.value) };
    webui.llm_policy = { ...(webui.llm_policy || {}), image_return_mode: els.agentImageReturnMode.value };
    webui.asset_policy = { ...(webui.asset_policy || {}), preview_max_edge: Number(els.agentPreviewMaxEdge.value), preview_quality: Number(els.agentPreviewQuality.value), lease_hours: Number(els.agentAssetRetentionHours.value) };
    webui.generation_defaults = { page: { text2img_model_ref: els.settingPageDefaultTextModel.value, img2img_model_ref: els.settingPageDefaultImageModel.value }, tool: { text2img_model_ref: els.settingToolDefaultTextModel.value, img2img_model_ref: els.settingToolDefaultImageModel.value } };
    return { base: { enable_llm_tool: els.settingTool.checked }, studio: webui };
  }

  function normalizeSettingsModelDefaults(payload) {
    (payload.webui?.providers || []).forEach((provider) => (provider.models || []).forEach((model) => ensureToolConfig(model, provider)));
  }

  function settingsFingerprint(value) {
    const copy = value ? JSON.parse(JSON.stringify(value)) : value;
    if (copy?.studio) { delete copy.studio.revision; if (copy.studio.ui) delete copy.studio.ui.settings_revision; }
    const normalize = (item) => Array.isArray(item) ? item.map(normalize) : item && typeof item === "object" ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, normalize(item[key])])) : item;
    return JSON.stringify(normalize(copy));
  }

  function updateSettingsDirty() {
    const dirty = !!state.settings && !!settingsBaseline && (settingsFingerprint(settingsDraft()) !== settingsBaseline || !!$("settingsView").querySelector("input:invalid, textarea:invalid, select:invalid"));
    els.saveSettingsButton.classList.toggle("is-dirty", dirty); $("settingsDirtyStatus").textContent = dirty ? "有未保存的更改" : state.settings ? "已保存" : "";
    renderStorageQuotas();
  }

  function dataUrlToFile(dataUrl, name) { const [head, encoded] = dataUrl.split(",", 2); const type = (head.match(/data:([^;]+)/) || [])[1] || "image/png"; const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0)); return new File([bytes], name, { type }); }
  async function exportSelected() { try { const result = await apiPost("gallery/export", { ids: Array.from(state.selectedIds) }); const client = await bridge(); await client.download(result.download_endpoint, {}, result.filename); showNotice("导出文件已开始下载。", "success"); } catch (error) { showNotice(errorMessage(error, "画廊导出失败"), "error"); } }
  async function deleteSelected() { if (!await confirmAction(`永久删除 ${state.selectedIds.size} 条生成记录及其结果图？`)) return; try { await apiPost("gallery/delete", { ids: Array.from(state.selectedIds) }); clearGallerySelection(); await loadGallery(); showNotice("所选生成记录已删除。", "success"); } catch (error) { showNotice(errorMessage(error, "生成记录删除失败"), "error"); } }
  async function useDataUrlAsReference(dataUrl, name) { try { const client = await bridge(); const uploaded = await client.upload("studio/reference/upload", dataUrlToFile(dataUrl, name)); state.references = [uploaded]; state.mode = "img2img"; document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === "img2img")); renderModelChoices(); renderReferences(); closeDetail(); switchView("generate"); setError(els.generationError, "已将当前成图作为新的图生图参考图。它不会被当作历史原始参考图。"); } catch (error) { setError(els.generationError, errorMessage(error, "添加参考图失败")); } }
  function confirmAction(message) { return new Promise((resolve) => { const dialog = $("confirmDialog"); const cancel = $("confirmCancel"); const accept = $("confirmAccept"); $("confirmMessage").textContent = message; dialog.classList.remove("is-hidden"); els.scrim.classList.remove("is-hidden"); syncPageScrollLock(); accept.focus(); const onKeydown = (event) => { if (event.key === "Escape") finish(false); }; const finish = (value) => { dialog.classList.add("is-hidden"); if (!els.detailDrawer.classList.contains("is-open")) els.scrim.classList.add("is-hidden"); syncPageScrollLock(); cancel.removeEventListener("click", onCancel); accept.removeEventListener("click", onAccept); document.removeEventListener("keydown", onKeydown); activeConfirmation = null; resolve(value); }; const onCancel = () => finish(false); const onAccept = () => finish(true); activeConfirmation = finish; cancel.addEventListener("click", onCancel); accept.addEventListener("click", onAccept); document.addEventListener("keydown", onKeydown); }); }

  function bindEvents() {
    if (eventsBound) return;
    eventsBound = true;
    library.bind();
    const refreshQuota = () => refreshProviderQuota();
    providerQuotaTimer = window.setInterval(refreshQuota, PROVIDER_QUOTA_TTL);
    document.addEventListener("visibilitychange", refreshQuota);
    window.addEventListener("focus", refreshQuota);
    window.addEventListener("pagehide", () => { window.clearInterval(providerQuotaTimer); providerQuotaTimer = 0; });
    window.addEventListener("pageshow", () => { if (!providerQuotaTimer) providerQuotaTimer = window.setInterval(refreshQuota, PROVIDER_QUOTA_TTL); refreshQuota(); });
    window.addEventListener("resize", () => { if (state.detailId) centerDetailFilmstrip(els.drawerBody.querySelector(".detail-filmstrip")); }, { passive: true });
    els.galleryGrid.addEventListener("click", (event) => { const card = event.target.closest("[data-gallery-id]"); if (card && !event.target.closest(".gallery-selection")) void openDetail(card.dataset.galleryId); });
    els.galleryGrid.addEventListener("change", (event) => { const input = event.target.closest("[data-select-id]"); if (!input) return; input.checked ? state.selectedIds.add(input.dataset.selectId) : state.selectedIds.delete(input.dataset.selectId); updateSelection(); });
    ["input", "change", "click"].forEach((name) => $("settingsView").addEventListener(name, () => window.setTimeout(updateSettingsDirty, 0)));
    $("parameterDialogApply").addEventListener("click", () => window.setTimeout(updateSettingsDirty, 0));
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
    document.querySelectorAll(".segment").forEach((button) => button.addEventListener("click", () => applyGenerationSelection(button.dataset.mode, state.defaultModelRefs[button.dataset.mode] || "")));
    document.querySelectorAll("[data-default-scope]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-default-scope]").forEach((item) => item.classList.toggle("is-active", item === button)); document.querySelectorAll("[data-default-panel]").forEach((panel) => panel.classList.toggle("is-hidden", panel.dataset.defaultPanel !== button.dataset.defaultScope)); }));
    els.agentImageReturnMode.addEventListener("change", syncAgentImageSettings);
    els.runMaintenanceButton.addEventListener("click", () => void runStorageMaintenance(false)); els.runDeepMaintenanceButton.addEventListener("click", () => void runStorageMaintenance(true));
    els.modelChoice.addEventListener("change", () => applyGenerationSelection(state.mode, els.modelChoice.value));
    els.resetNegativePromptButton.addEventListener("click", () => { els.negativePrompt.value = selectedModel()?.negative_prompt_default || ""; els.negativePrompt.focus(); });
    $("referenceChooseButton").addEventListener("click", () => els.referenceUpload.click());
    els.referenceUpload.addEventListener("change", async () => { setError(els.generationError, ""); try { await uploadReferences(els.referenceUpload.files); } catch (error) { setError(els.generationError, errorMessage(error, "上传参考图失败")); } finally { els.referenceUpload.value = ""; } });
    els.generationForm.addEventListener("submit", generate); $("galleryRefresh").addEventListener("click", () => void loadGallery()); els.galleryPrev.addEventListener("click", () => void loadGallery(state.galleryPage - 1)); els.galleryNext.addEventListener("click", () => void loadGallery(state.galleryPage + 1)); els.gallerySearch.addEventListener("change", () => void loadGallery(0)); els.galleryProvider.addEventListener("change", () => void loadGallery(0)); els.galleryMode.addEventListener("change", () => void loadGallery(0)); els.gallerySource.addEventListener("change", () => void loadGallery(0));
    $("cancelSelectionButton").addEventListener("click", clearGallerySelection); $("selectAllButton").addEventListener("click", () => { state.galleryItems.forEach((item) => state.selectedIds.add(item.id)); els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => { input.checked = true; }); updateSelection(); }); $("exportButton").addEventListener("click", () => void exportSelected()); $("deleteButton").addEventListener("click", () => void deleteSelected());
    $("closeDrawer").addEventListener("click", closeDetail); $("closeImagePreview").addEventListener("click", closeImagePreview); els.imagePreviewPrev.addEventListener("click", () => void navigateImagePreview(-1)); els.imagePreviewNext.addEventListener("click", () => void navigateImagePreview(1)); bindImagePreviewGestures(); els.imagePreview.querySelector("[data-close-image-preview]").addEventListener("click", closeImagePreview); els.previewImage.addEventListener("click", () => { if (Date.now() - state.imagePreviewSwipeAt < 500) return; closeImagePreview(); }); els.scrim.addEventListener("click", () => { if (!els.parameterDialog.classList.contains("is-hidden")) return; if (activeConfirmation) activeConfirmation(false); else closeDetail(); }); $("parameterDialogCancel").addEventListener("click", closeToolParameterDialog); $("parameterDialogApply").addEventListener("click", applyToolParameterDialog); els.addProviderButton.addEventListener("click", () => void addProvider()); els.addModelButton.addEventListener("click", () => void addModel()); els.saveSettingsButton.addEventListener("click", () => void saveSettings());
    document.addEventListener("keydown", (event) => { if (mobileImageViewer || library.modalOpen()) return; if (event.key === "Escape") { if (!els.parameterDialog.classList.contains("is-hidden")) closeToolParameterDialog(); else if (!els.imagePreview.classList.contains("is-hidden")) closeImagePreview(); else if (els.detailDrawer.classList.contains("is-open") && !activeConfirmation) closeDetail(); return; } if (event.target.closest('input,textarea,select,[role="combobox"],[contenteditable=true]')) return; if (!els.imagePreview.classList.contains("is-hidden")) { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); void navigateImagePreview(event.key === "ArrowLeft" ? -1 : 1); } return; } if (!els.detailDrawer.classList.contains("is-open") || activeConfirmation) return; if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); void navigateDetail(event.key === "ArrowLeft" ? -1 : 1); } });
  }

  const library = window.ImageStudioLibrary({ state, escape, apiGet, apiPost, bridge, showNotice, errorMessage, formatDate, formatBytes, sourceLabel, syncPageScrollLock, switchView, requestParameters, loadGallery, clearGallerySelection, openDetail, closeDetail, reproduce, applyDraft, useDataUrlAsReference, ensureDetailMetadata, ensureDetailPreview, getImageMedia, cacheImageMedia, loadImageMedia });

  async function start() {
    bindEvents();
    try { await bootstrap(); }
    catch (error) { const message = errorMessage(error, "页面初始化失败"); els.runtimeStatus.textContent = "页面初始化失败"; showNotice(message, "error"); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => void start(), { once: true });
  else void start();
})();
