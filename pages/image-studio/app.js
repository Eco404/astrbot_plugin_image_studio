(function () {
  "use strict";

  window.__imageStudioAppLoaded = true;

  const state = {
    view: "generate", mode: "text2img", providers: [], models: [], comfyuiTemporaryModel: null, selectedProviderId: "", selectedModelRef: "", defaultModelRefs: { text2img: "", img2img: "" }, parameterValues: {}, parameterCarry: {}, negativePromptCarry: "", hasNegativePromptCarry: false, references: [],
    resultImages: [], galleryItems: [], galleryPage: 0, galleryLimit: 24, galleryTotal: 0, selectedIds: new Set(), detailId: "", detailData: null, detailFallbackThumbnail: "", detailImageIndex: 0, detailRequestedImageIndex: 0, detailAssetsLoaded: false, detailNavigating: false, imagePreviewItems: [], imagePreviewIndex: 0, imagePreviewTitle: "图片预览", imagePreviewDownloadFilename: "", imagePreviewContext: null, imagePreviewNavigating: false, imagePreviewSwipeAt: 0,
  };
  let activeConfirmation = null;
  let referencesUploading = false;
  let eventsBound = false;
  const providerQuotas = new Map();
  const PROVIDER_QUOTA_TTL = 30_000;
  let providerQuotaTimer = 0;
  let galleryRequestRevision = 0;
  let galleryQueryKey = "";
  let galleryDisplayedPageKey = "";
  let galleryPreviewSession = null;
  let galleryPreviewActive = 0;
  const GALLERY_PREVIEW_CONCURRENCY = 4;
  const galleryFilterFields = [["galleryProvider", "provider_ids"], ["galleryMode", "modes"], ["gallerySource", "sources"], ["galleryEngine", "generation_engines"]];
  const galleryFilterSelections = new Map();
  let gallerySort = "created";
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
  let decodedDisplayBytes = 0;
  let mobileImageViewer = null;
  let mobileImageSequence = [];
  let mobileImageDataSource = [];
  let mobileImageLoads = new Map();
  let mobileViewerSession = null;
  let mobileViewerOpenRevision = 0;
  let mobileDetailSyncRevision = 0;
  let mobileViewerOpening = false;
  let suppressMobileDetailSync = false;
  const EMPTY_MOBILE_IMAGE = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=";
  const $ = (id) => document.getElementById(id);
  const els = {
    pageTitle: $("pageTitle"), pageSubtitle: $("pageSubtitle"), runtimeStatus: $("runtimeStatus"), providerStatus: $("providerStatus"), providerStatusName: $("providerStatusName"), providerQuota: $("providerQuota"),
    modelChoice: $("modelChoice"), modelProvider: $("modelProvider"), comfyWorkflowWorkspace: $("comfyWorkflowWorkspace"), comfyWorkflowChoice: $("comfyWorkflowChoice"), workspaceEmpty: $("workspaceEmpty"), generatorWorkspace: $("generatorWorkspace"), modelParameters: $("modelParameters"), referenceField: $("referenceField"), referenceUpload: $("referenceUpload"), referenceStrip: $("referenceStrip"),
    generationForm: $("generationForm"), prompt: $("prompt"), negativePromptField: $("negativePromptField"), negativePrompt: $("negativePrompt"), negativePromptHint: $("negativePromptHint"), resetNegativePromptButton: $("resetNegativePromptButton"), advancedParameters: $("advancedParameters"), parameters: $("parameters"), generationError: $("generationError"), generateButton: $("generateButton"), resultEmpty: $("resultEmpty"), resultGrid: $("resultGrid"), resultMeta: $("resultMeta"),
    galleryGrid: $("galleryGrid"), galleryEmpty: $("galleryEmpty"), galleryPagination: $("galleryPagination"), galleryPrev: $("galleryPrev"), galleryNext: $("galleryNext"), galleryPageLabel: $("galleryPageLabel"), gallerySearch: $("gallerySearch"), galleryProvider: $("galleryProvider"), galleryMode: $("galleryMode"), gallerySource: $("gallerySource"), selectionBar: $("selectionBar"), selectionCount: $("selectionCount"),
    detailDrawer: $("detailDrawer"), drawerBody: $("drawerBody"), detailDate: $("detailDate"), scrim: $("scrim"), imagePreview: $("imagePreview"), imagePreviewBody: $("imagePreviewBody"), imagePreviewPrev: $("imagePreviewPrev"), imagePreviewNext: $("imagePreviewNext"), imagePreviewDots: $("imagePreviewDots"), previewImage: $("previewImage"), imagePreviewTitle: $("imagePreviewTitle"), downloadImageButton: $("downloadImageButton"),
    parameterDialog: $("parameterDialog"),
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
    return `${identity}:${detail}:${detail === "preview" || detail.startsWith("display:") ? image.thumbnail_revision || "legacy" : "original"}`;
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
      if (image._displayDataUrl === source) { delete image._displayDataUrl; delete image._displayEdge; }
      if (image.thumbnail_data_url === source) delete image.thumbnail_data_url;
    }
    for (const item of mobileViewerSession?.items || []) {
      if (item.originalSrc === source) item.originalSrc = "";
      if (item.displaySrc === source) item.displaySrc = "";
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
      const params = detail.startsWith("display:") ? { detail: "display", max_edge: Number(detail.split(":")[1]) } : { detail };
      const promise = apiGet(`gallery/image/${id}`, params).then(payload => {
        if (!payload?.data_url) throw new Error("图片接口没有返回可用内容。");
        // Store under the requested revision only: a late request must never
        // overwrite a newer preview after thumbnail settings were changed.
        return cacheImageMedia(image, detail, payload.data_url);
      }).finally(() => { if (imageMediaLoads.get(key) === promise) imageMediaLoads.delete(key); });
      imageMediaLoads.set(key, promise);
    }
    return imageMediaLoads.get(key);
  }

  function touchImageDisplay() { return window.ImageStudioDetailSwipe.usesTouchInteraction(); }

  function displayImageEdge(image, area = null) {
    const width = Math.max(1, Number(image?.width) || 1), height = Math.max(1, Number(image?.height) || 1);
    const fit = Math.min((area?.width || window.innerWidth) / width, (area?.height || window.innerHeight) / height, 1);
    const needed = Math.min(Math.max(width, height), Math.ceil(Math.max(width, height) * fit * Math.min(3, window.devicePixelRatio || 1)));
    return [768, 1024, 1536, 2048].find(edge => edge >= needed) || 2048;
  }

  function detailDisplayReady(image) {
    return touchImageDisplay() ? !!image?._displayDataUrl && image._displayEdge >= displayImageEdge(image) : !!image?._originalLoaded;
  }

  function reuseImageMedia(detail) {
    for (const image of detail?.images || []) {
      image.thumbnail_data_url ||= getImageMedia(image, "preview");
      if (touchImageDisplay()) {
        const edge = displayImageEdge(image);
        const display = getImageMedia(image, `display:${edge}`);
        if (display) { image._displayDataUrl = display; image._displayEdge = edge; }
        continue;
      }
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
  function generationModels() {
    const temporary = state.comfyuiTemporaryModel;
    return temporary && state.providers.some(provider => provider.id === temporary.provider_id && provider.kind === "comfyui" && provider.enabled !== false) ? [...state.models, temporary] : state.models;
  }
  function modelsForMode() { const current = selectedModel(); return generationModels().map(item => item.model_ref === current?.model_ref ? current : item).filter((item) => state.mode === "text2img" ? item.supports_text2img : referenceLimitForModel(item) > 0); }
  function selectedModel() {
    const model = generationModels().find(item => item.model_ref === state.selectedModelRef) || null;
    const snapshot = state.comfyuiModelOverride;
    if (model && snapshot?.model_ref === model.model_ref) return { ...model, ...snapshot.model, model_ref: model.model_ref, provider_id: model.provider_id, provider_name: model.provider_name, provider_kind: model.provider_kind };
    return model;
  }
  function selectedProvider() { const model = selectedModel(); return state.providers.find((item) => item.id === (model?.provider_id || state.selectedProviderId)) || null; }
  function text(value) { return value === null || value === undefined ? "" : String(value); }
  function escape(value) { const div = document.createElement("div"); div.textContent = text(value); return div.innerHTML.replaceAll('"', "&quot;").replaceAll("'", "&#39;"); }
  function formatDate(value) { return new Date(Number(value) * 1000).toLocaleString(); }
  function formatBytes(value) { const bytes = Number(value || 0); return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(0, Math.round(bytes / 1024))} KB`; }
  function sourceLabel(value) { return ({ webui: "WebUI", command: "指令", llm_tool: "LLM 工具", import: "导入", external: "外部图库" })[value] || value || "未知"; }
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
    return state.view === "generate" && selectedModel() && ["nai_direct", "novelai_official"].includes(provider?.kind) ? provider : null;
  }

  function renderProviderStatus() {
    const model = selectedModel(), provider = selectedProvider(), active = quotaProvider();
    els.providerStatusName.textContent = model && provider ? `${model.name} · ${provider.name}` : "未选择模型";
    els.providerStatusName.dataset.tooltip = els.providerStatusName.textContent;
    els.providerStatusName.dataset.tooltipOverflow = "";
    els.providerStatus.classList.toggle("has-provider-quota", !!active);
    els.providerQuota.hidden = !active;
    if (!active) { els.providerQuota.textContent = ""; delete els.providerQuota.dataset.tooltip; return; }
    const quota = providerQuotas.get(active.id);
    const failed = !!quota?.error;
    els.providerQuota.classList.toggle("is-unavailable", failed);
    if (active.kind === "novelai_official") {
      const data = quota?.data, usage = data?.usage;
      const showV5Usage = model?.id?.startsWith("nai-diffusion-5-") === true;
      const amount = value => value === null || value === undefined ? "未知" : value.toLocaleString("zh-CN");
      const allowance = usage?.is_negative === true ? "已用尽" : usage?.percent === null || usage?.percent === undefined ? "未知" : `${Math.max(0, usage.percent)}%`;
      els.providerQuota.classList.toggle("is-warning", !!data && showV5Usage && usage?.is_negative === true);
      els.providerQuota.textContent = failed ? "额度暂不可用" : data ? `Anlas ${amount(data.remaining)}${showV5Usage ? ` · V5 ${allowance}` : ""}${data.subscription_active ? "" : " · 未订阅"}` : "额度查询中…";
      const availability = usage?.is_negative === false ? "可用" : usage?.is_negative === true ? "已用尽" : "状态未知";
      const refill = usage?.time_until_next_percent === null || usage?.time_until_next_percent === undefined ? "" : `\n距离下一个百分比恢复约 ${usage.time_until_next_percent} 秒`;
      const usageDetail = showV5Usage ? `\nV5 免费额度：${allowance}（${availability}），与 Anlas 余额独立${refill}` : "";
      els.providerQuota.dataset.tooltip = failed ? quota.error : data ? `服务商：${active.name}\n订阅：${data.subscription_active ? "有效" : "未订阅"}\nAnlas：订阅 ${amount(data.subscription_anlas)}，购买 ${amount(data.purchased_anlas)}${usageDetail}${data.subscription_active ? "" : "\n未订阅不代表服务商已停用；Anlas 余额不代表免费试用剩余次数。"}\n更新于 ${formatDate(data.checked_at)}` : `正在查询 ${active.name} 的额度`;
      return;
    }
    els.providerQuota.classList.toggle("is-warning", !!quota?.data && (!quota.data.enabled || quota.data.remaining === 0));
    els.providerQuota.textContent = failed ? "额度暂不可用" : quota?.data ? `剩余额度 ${quota.data.remaining.toLocaleString("zh-CN")}${quota.data.enabled ? "" : " · 已停用"}` : "额度查询中…";
    els.providerQuota.dataset.tooltip = failed ? quota.error : quota?.data ? `服务商：${active.name}\n更新于 ${formatDate(quota.data.checked_at)}` : `正在查询 ${active.name} 的额度`;
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
      const nullableAmount = value => value === null || Number.isSafeInteger(value) && value >= 0;
      const nullableNumber = value => value === null || Number.isFinite(value) && value >= 0;
      const usage = payload?.usage;
      const validQuota = provider.kind === "novelai_official"
        ? payload?.kind === "novelai_official" && typeof payload.subscription_active === "boolean" && nullableAmount(payload.tier)
          && [payload.remaining, payload.subscription_anlas, payload.purchased_anlas].every(nullableAmount)
          && (usage === null || usage && typeof usage === "object" && (usage.percent === null || Number.isFinite(usage.percent)) && (usage.is_negative === null || typeof usage.is_negative === "boolean") && nullableNumber(usage.time_until_next_percent))
        : Number.isSafeInteger(payload?.remaining) && payload.remaining >= 0 && typeof payload.enabled === "boolean";
      if (payload?.provider_id !== provider.id || !validQuota || !Number.isFinite(payload.checked_at)) throw new Error("额度查询返回的数据格式不正确");
      entry.data = payload;
    }).catch((error) => { entry.error = errorMessage(error, "额度查询失败"); }).finally(() => {
      entry.pending = false; entry.updatedAt = Date.now();
      // A forced refresh or settings reload can supersede an earlier query.
      if (providerQuotas.get(provider.id) === entry) renderProviderStatus();
    });
  }

  function syncPageScrollLock() {
    const motion = window.ImageStudioDialogMotion;
    const foreground = element => !element.classList.contains("is-hidden") && !element.classList.contains("is-closing");
    const needsScrim = els.detailDrawer.classList.contains("is-open") || foreground(els.parameterDialog) || foreground($('confirmDialog'));
    if (needsScrim) motion.show(els.scrim);
    else if (foreground(els.scrim)) motion.hide(els.scrim, syncPageScrollLock);
    const locked = els.detailDrawer.classList.contains("is-open")
      || !els.imagePreview.classList.contains("is-hidden")
      || !!mobileImageViewer
      || !els.parameterDialog.classList.contains("is-hidden")
      || !$('studioModalRoot').classList.contains("is-hidden")
      || !$('confirmDialog').classList.contains("is-hidden")
      || !els.scrim.classList.contains("is-hidden");
    document.documentElement.classList.toggle("modal-open", locked);
  }

  function switchView(view) {
    if (view !== "settings" && state.view === "settings" && (settings.isSaving() || settings.settingsDirty())) {
      void settings.leaveSettings(view);
      return;
    }
    showView(view);
  }

  function showView(view) {
    window.ImageStudioSelect?.close();
    state.view = view;
    if (view !== "gallery") stopGalleryPreviews();
    document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
    document.querySelectorAll(".view").forEach((item) => item.classList.toggle("is-active", item.id === `${view}View`));
    const labels = { generate: ["生图", "选择模式和模型后开始创作"], gallery: ["画廊", "搜索、筛选、复现或导出历史生成记录"], import: ["导入", "图片与生成参数"], settings: ["设置", "管理运行策略、历史、生图服务商和模型"], };
    els.pageTitle.textContent = labels[view][0]; els.pageSubtitle.textContent = labels[view][1];
    refreshProviderQuota();
    library.syncFloatingBars();
    if (view === "gallery") void loadGallery();
    if (view === "settings") { library.layoutSettingsPanels(); void loadSettings(); void loadStorageHealth(); }
    externalSources.viewChanged();
    window.ImageStudioSelect?.refresh();
  }

  function collectModelParameters() {
    novelaiControls.collect();
    const values = {};
    effectiveModelParameters(selectedModel()).forEach(([name, descriptor]) => {
      if (descriptor.webui_visible === false && Object.prototype.hasOwnProperty.call(state.parameterValues, name)) values[name] = state.parameterValues[name];
    });
    els.modelParameters.querySelectorAll("[data-model-parameter]").forEach((input) => {
      if (input.dataset.unsetValue === "true") return;
      const key = input.dataset.modelParameter;
      if (input.dataset.nullValue === "true") values[key] = null;
      else if (input.type === "checkbox") values[key] = input.checked;
      else if (input.dataset.parameterType === "number") values[key] = input.value === "" ? "" : comfyuiControls.active(selectedModel()) ? comfyuiControls.numericValue(input.value) : Number(input.value);
      else if (input.dataset.parameterType === "json") { try { values[key] = input.value.trim() ? JSON.parse(input.value) : {}; } catch { values[key] = input.value; } }
      else if (input.dataset.parameterType === "select" && comfyuiControls.active(selectedModel())) { const choices = selectedModel()?.parameters?.[key]?.choices || []; const matched = choices.find(choice => String(typeof choice === "object" ? choice.value : choice) === input.value); values[key] = matched === undefined ? input.value : typeof matched === "object" ? matched.value : matched; }
      else values[key] = input.value;
    });
    return values;
  }

  function schemaParameterTitle(name, descriptor) {
    const fieldName = String(name).trim();
    const label = String(descriptor.label || "").trim();
    return label || fieldName;
  }

  function schemaParameterTooltip(name, descriptor) {
    const requestKey = String(descriptor.request_key || "").trim() || String(name).trim();
    const description = String(descriptor.description || "").trim();
    return description ? `${requestKey}\n${description}` : requestKey;
  }

  function schemaParameterLabel(name, descriptor, tag = "label") {
    const label = escape(schemaParameterTitle(name, descriptor));
    const tooltip = escape(schemaParameterTooltip(name, descriptor));
    const textTag = tag === "strong" ? "strong" : "span";
    if (tooltip === label) return `<span class="schema-parameter-label"><${tag}>${label}</${tag}></span>`;
    return `<span class="schema-parameter-label"><button type="button" class="parameter-help-text" data-tooltip="${tooltip}" data-tooltip-toggle aria-label="查看${label}说明"><${textTag}>${label}</${textTag}></button></span>`;
  }

  function renderModelParameter(name, descriptor) {
    const type = String(descriptor.type || "text").toLowerCase();
    const label = schemaParameterLabel(name, descriptor);
    const accessibleLabel = escape(schemaParameterTitle(name, descriptor));
    let value = Object.prototype.hasOwnProperty.call(state.parameterValues, name) ? state.parameterValues[name] : descriptor.default ?? "";
    const specialized = novelaiControls.parameter(name, descriptor, value);
    if (specialized !== null) return specialized;
    const requestKey = escape(descriptor.request_key || name);
    if (type === "preset" && Array.isArray(descriptor.choices)) {
      const options = descriptor.choices.map((choice) => `<option value="${escape(choice.value)}" ${String(choice.value) === String(value) ? "selected" : ""}>${escape(choice.label || choice.value)}</option>`).join("");
      return `<div class="field">${label}<select aria-label="${accessibleLabel}" data-model-parameter="${escape(name)}" data-parameter-type="preset" data-preset-target="${escape(descriptor.target || "")}" data-ui-only="true">${options}</select></div>`;
    }
    if (type === "select" && Array.isArray(descriptor.choices)) {
      const selected = descriptor.choices.some((choice) => String(typeof choice === "object" ? choice.value : choice) === String(value));
      const options = `${selected ? "" : '<option value="" selected>未设置</option>'}${descriptor.choices.map((choice) => { const option = typeof choice === "object" ? choice : { value: choice, label: choice }; return `<option value="${escape(option.value)}" ${String(option.value) === String(value) ? "selected" : ""}>${escape(option.label)}</option>`; }).join("")}`;
      return `<div class="field">${label}<select aria-label="${accessibleLabel}" data-model-parameter="${escape(name)}" data-parameter-type="select" data-request-key="${requestKey}">${options}</select></div>`;
    }
    if (type === "boolean" || type === "bool") return `<div class="toggle-row">${label}<label class="toggle-control"><input aria-label="${accessibleLabel}" data-model-parameter="${escape(name)}" data-request-key="${requestKey}" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`;
    if (type === "json" || type === "object") return `<div class="field field-wide">${label}<textarea aria-label="${accessibleLabel}" data-model-parameter="${escape(name)}" data-parameter-type="json" data-request-key="${requestKey}" rows="3" spellcheck="false">${escape(typeof value === "string" ? value : JSON.stringify(value || {}, null, 2))}</textarea></div>`;
    if (type === "textarea") return `<div class="field field-wide">${label}<textarea aria-label="${accessibleLabel}" data-model-parameter="${escape(name)}" data-parameter-type="text" data-request-key="${requestKey}" rows="4">${escape(value)}</textarea></div>`;
    const inputType = ["number", "int", "integer", "float"].includes(type) ? "number" : "text";
    const min = descriptor.min !== undefined ? ` min="${escape(descriptor.min)}"` : "";
    const max = descriptor.max !== undefined ? ` max="${escape(descriptor.max)}"` : "";
    const step = descriptor.step !== undefined ? ` step="${escape(descriptor.step)}"` : inputType === "number" ? " step=\"any\"" : "";
    return `<div class="field">${label}<input aria-label="${accessibleLabel}" data-model-parameter="${escape(name)}" data-parameter-type="${inputType === "number" ? "number" : "text"}" data-request-key="${requestKey}" type="${inputType}" value="${escape(value)}"${min}${max}${step} /></div>`;
  }

  function renderModelChoices() {
    const available = modelsForMode();
    if (!available.some((item) => item.model_ref === state.selectedModelRef)) state.selectedModelRef = "";
    const comfyProviders = new Map(state.providers.filter(item => item.kind === "comfyui" && item.enabled !== false).map(item => [item.id, item.name]));
    const model = selectedModel();
    if (model) state.selectedProviderId = model.provider_id;
    else if (!comfyProviders.has(state.selectedProviderId)) state.selectedProviderId = "";
    const listed = new Set();
    const options = available.map(item => {
      if (item.provider_kind !== "comfyui") return `<option value="${escape(item.model_ref)}">${escape(item.name)} · ${escape(item.provider_name)}</option>`;
      if (listed.has(item.provider_id)) return "";
      listed.add(item.provider_id);
      return `<option value="${escape(`@comfy:${item.provider_id}`)}">${escape(item.provider_name)} · ComfyUI</option>`;
    }).join("") + Array.from(comfyProviders).filter(([id]) => !listed.has(id)).map(([id, name]) => `<option value="${escape(`@comfy:${id}`)}">${escape(name || id)} · ComfyUI</option>`).join("");
    els.modelChoice.innerHTML = options ? `<option value="">请选择模型或 ComfyUI</option>${options}` : '<option value="">当前模式没有可用模型</option>';
    els.modelChoice.disabled = !options;
    const comfy = comfyProviders.has(state.selectedProviderId);
    els.modelChoice.value = comfy ? `@comfy:${state.selectedProviderId}` : state.selectedModelRef;
    document.querySelector('.model-select-row > label').textContent = comfy ? "服务商" : "模型";
    const workflows = comfy ? available.filter(item => item.provider_id === state.selectedProviderId && item.provider_kind === "comfyui") : [];
    $("generationSelectors").classList.toggle("has-workflow", comfy);
    els.comfyWorkflowWorkspace.classList.toggle("is-hidden", !comfy);
    els.comfyWorkflowChoice.disabled = !workflows.length;
    els.comfyWorkflowChoice.innerHTML = '<option value="">请选择工作流</option>' + workflows.map(item => `<option value="${escape(item.model_ref)}">${escape(item.name || item.id)}${item.temporary ? " · 临时" : ""}</option>`).join("");
    els.comfyWorkflowChoice.value = comfy ? state.selectedModelRef : "";
    els.workspaceEmpty.textContent = comfy ? "请选择工作流" : "请选择模型";
    els.modelProvider.textContent = comfy ? `${workflows.length} 个工作流` : model?.provider_name || "";
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
    comfyuiControls.renderWorkspace(model);
    els.generatorWorkspace.disabled = !model;
    els.workspaceEmpty.classList.toggle("is-hidden", !!model);
    els.generatorWorkspace.classList.toggle("is-hidden", !model);
    if (!model) {
      els.modelParameters.innerHTML = "";
      renderGenerationForm();
      return;
    }
    els.prompt.closest(".field").classList.toggle("is-hidden", !comfyuiControls.promptRequired(model));
    els.prompt.setAttribute("aria-required", String(comfyuiControls.promptRequired(model)));
    els.modelParameters.innerHTML = effectiveModelParameters(model).filter(([, descriptor]) => descriptor.webui_visible !== false && parameterAppliesToMode(descriptor)).map(([name, descriptor]) => renderModelParameter(name, descriptor)).join("") || '<div class="workspace-placeholder">该模型没有额外参数。</div>';
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
    novelaiControls.bindParameters();
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

  function applyGenerationSelection(mode, modelRef, providerId = "") {
    const previousModel = selectedModel(); if (previousModel?.supports_negative_prompt) { state.negativePromptCarry = els.negativePrompt.value; state.hasNegativePromptCarry = true; }
    const carried = carriedParameterValues();
    state.comfyuiSnapshot = null; state.comfyuiModelOverride = null;
    state.parameterCarry = carried; state.mode = mode; state.selectedModelRef = modelRef || "";
    state.selectedProviderId = selectedModel()?.provider_id || providerId;
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
    if (novelaiControls.renderReferences(els.referenceStrip, index => { state.references.splice(index, 1); renderReferences(); })) return;
    els.referenceStrip.innerHTML = state.references.map((item, index) => `<div class="reference-item"><img src="${item.preview_data_url}" alt="参考图 ${index + 1}" /><button type="button" data-reference-index="${index}" aria-label="移除参考图"><span aria-hidden="true">×</span></button></div>`).join("");
    els.referenceStrip.querySelectorAll("[data-reference-index]").forEach((button) => button.addEventListener("click", () => { state.references.splice(Number(button.dataset.referenceIndex), 1); renderReferences(); }));
  }

  async function bootstrap() {
    const payload = await apiGet("studio/bootstrap");
    state.comfyuiSnapshot = null; state.comfyuiModelOverride = null;
    settings.setNovelAIModels(payload.novelai_models);
    providerQuotas.clear();
    state.providers = Array.isArray(payload.providers) ? payload.providers : [];
    state.models = Array.isArray(payload.models) ? payload.models : [];
    state.parameterValues = {}; state.parameterCarry = {}; state.negativePromptCarry = ""; state.hasNegativePromptCarry = false;
    state.defaultModelRefs = { text2img: payload.defaults?.text2img_model_ref || "", img2img: payload.defaults?.img2img_model_ref || "" };
    state.selectedModelRef = state.defaultModelRefs[state.mode] || "";
    state.selectedProviderId = selectedModel()?.provider_id || "";
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
    if (comfyuiControls.promptRequired(selectedModel()) && !els.prompt.value.trim()) { setError(els.generationError, "请填写提示词。"); return; }
    if (referencesUploading) { setError(els.generationError, "请等待参考图上传完成。"); return; }
    let parameters = {};
    if (els.parameters.value.trim()) {
      try { parameters = JSON.parse(els.parameters.value); } catch { setError(els.generationError, "高级参数必须是合法 JSON"); return; }
    }
    const model = selectedModel();
    const provider = selectedProvider();
    if (!model || !provider) { setError(els.generationError, "请先选择支持当前模式的模型"); return; }
    if (state.mode === "img2img" && !state.references.length) { setError(els.generationError, "图生图需要至少一张参考图"); return; }
    const novelaiError = novelaiControls.validate();
    if (novelaiError) { setError(els.generationError, novelaiError); return; }
    const schema = model.parameters || {};
    const mappedParameters = Object.fromEntries(Object.entries(collectModelParameters()).map(([name, value]) => [schema[name]?.request_key || name, value]));
    for (const [name, descriptor] of effectiveModelParameters(model)) {
      const key = descriptor.request_key || name;
      if (!parameterAppliesToMode(descriptor)) { delete parameters[key]; delete parameters[name]; delete mappedParameters[key]; }
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
      const request = { mode: state.mode, provider_id: provider.id, model_ref: model.model_ref, prompt: els.prompt.value, negative_prompt: els.negativePrompt.value, model: model.id, size, count: Number(count), parameters: { ...parameters, ...mappedParameters }, reference_ids: state.references.map((item) => item.id), ...(provider.kind === "comfyui" && state.comfyuiSnapshot ? { comfyui: state.comfyuiSnapshot } : {}) };
      if (provider.kind === "comfyui") {
        if (model.temporary) request.temporary_model = comfyuiControls.temporaryModel(model);
        await comfyuiControls.submit(request); showNotice("工作流任务已提交，可展开任务队列查看进度。", "success"); return;
      }
      const result = await apiPost("studio/generate", request);
      state.resultImages = result.images || []; state.references = [];
      for (const [name, descriptor] of effectiveModelParameters(model)) if ((descriptor.request_key || name) === "reference_settings") state.parameterValues[name] = [];
      renderReferences();
      renderGenerationResult(result);
    } catch (error) { setError(els.generationError, errorMessage(error, "生成失败")); }
    finally {
      els.generateButton.disabled = false; els.generateButton.textContent = "生成图片";
      if (["nai_direct", "novelai_official"].includes(provider.kind)) { providerQuotas.delete(provider.id); refreshProviderQuota(); }
    }
  }

  function renderGenerationResult(result) {
    state.resultImages = result.images || [];
    els.resultEmpty.classList.toggle("is-hidden", state.resultImages.length > 0);
    els.resultGrid.innerHTML = state.resultImages.map((image, index) => `<div class="result-card"><div class="result-frame"><img class="result-image-backdrop" src="${escape(image.data_url)}" alt="" aria-hidden="true" /><img class="result-image" src="${escape(image.data_url)}" alt="生成结果" data-result-preview="${index}" /></div><div class="result-card-actions"><button class="quiet-button" data-result-reference="${index}" type="button">用作参考图</button></div></div>`).join("");
    els.resultGrid.querySelectorAll("[data-result-reference]").forEach(button => button.addEventListener("click", () => void useDataUrlAsReference(state.resultImages[Number(button.dataset.resultReference)].data_url, "generated-reference.png")));
    els.resultGrid.querySelectorAll("[data-result-preview]").forEach(image => image.addEventListener("click", () => { const index = Number(image.dataset.resultPreview); openImagePreview(image.src, `生成结果-${index + 1}`, state.resultImages[index]?.download_filename, state.resultImages, index); }));
    els.resultMeta.textContent = `${result.provider_name || "ComfyUI"} · ${result.model || "工作流"} · ${(Number(result.elapsed_ms || 0) / 1000).toFixed(1)} 秒${result.generation_id ? " · 已保存到画廊" : " · 历史未保留"}`;
    if (result.warning) { setError(els.generationError, result.warning); showNotice(result.warning); }
  }

  function viewComfyResult(result) {
    renderGenerationResult(result);
    const first = state.resultImages[0];
    if (first?.data_url) openImagePreview(first.data_url, "生成结果-1", first.download_filename, state.resultImages, 0);
  }

  async function loadGallery(page = state.galleryPage) {
    library.closeGalleryPagePicker();
    if (state.view === "gallery") state.galleryLimit = library.galleryPageSize();
    const requestedPage = Math.max(0, Number.isFinite(Number(page)) ? Math.floor(Number(page)) : 0);
    const filters = galleryFilters();
    const queryKey = JSON.stringify({ ...filters, limit: state.galleryLimit });
    const pageKey = `${queryKey}:${requestedPage}`;
    const revision = ++galleryRequestRevision;
    const preserveCards = pageKey === galleryDisplayedPageKey;
    if (!preserveCards) stopGalleryPreviews();
    if (queryKey !== galleryQueryKey) state.galleryTotal = 0;
    galleryQueryKey = queryKey;
    state.galleryPage = requestedPage;
    if (!preserveCards) {
      state.galleryItems = [];
      galleryDisplayedPageKey = "";
      const remaining = state.galleryTotal - requestedPage * state.galleryLimit;
      const count = Math.max(1, Math.min(state.galleryLimit, remaining > 0 ? remaining : state.galleryLimit));
      els.galleryGrid.innerHTML = Array.from({ length: count }, () => library.renderGalleryPlaceholder()).join("");
      els.galleryGrid.querySelectorAll(".gallery-image-pending").forEach(mount => mount.append(window.ImageStudioImagePlaceholder.create()));
      els.galleryEmpty.classList.add("is-hidden");
    }
    setGalleryLoading(true);
    updateGalleryPagination();
    try {
      const payload = await apiGet("gallery/list", { ...filters, light: 1, limit: state.galleryLimit, offset: requestedPage * state.galleryLimit });
      if (revision !== galleryRequestRevision) return false;
      const total = Math.max(0, Number(payload.total || 0));
      const limit = Math.max(1, Number(payload.limit || state.galleryLimit));
      const totalPages = Math.max(1, Math.ceil(total / limit));
      if (total > 0 && requestedPage >= totalPages) { state.galleryPage = totalPages - 1; return await loadGallery(state.galleryPage); }
      state.galleryPage = Math.min(requestedPage, totalPages - 1); state.galleryLimit = limit; state.galleryTotal = total;
      const dataChanged = payload.revision ? observeGalleryRevision(payload.revision) : true;
      if (!payload.revision) invalidateBrowseCache();
      state.galleryItems = payload.items || []; renderGallery(payload);
      galleryDisplayedPageKey = `${queryKey}:${state.galleryPage}`;
      setGalleryLoading(false);
      startGalleryPreviews();
      if (detailNavigationSession?.active && browseFilterKey(detailNavigationSession.filters) !== browseFilterKey(galleryFilters())) refreshDetailSequence(detailNavigationSession);
      else if (dataChanged && detailNavigationSession?.active) void warmDetailNeighbors(detailNavigationSession);
      return true;
    } catch (error) {
      if (revision !== galleryRequestRevision) return false;
      setGalleryLoading(false, preserveCards ? "刷新失败，仍显示上次的内容。" : "画廊加载失败，请重试。");
      if (!preserveCards) els.galleryGrid.replaceChildren();
      else startGalleryPreviews();
      if (state.view === "gallery") showNotice(errorMessage(error, "画廊加载失败"), "error");
      return false;
    }
  }

  function setGalleryLoading(loading, error = "") {
    els.galleryGrid.setAttribute("aria-busy", String(loading));
    $("galleryLoadingStatus").textContent = error;
    $("galleryLoadState").hidden = !error;
    $("galleryRetry").hidden = !error;
    $("selectAllButton").disabled = loading;
  }

  function updateGalleryPagination() {
    const totalPages = Math.max(1, Math.ceil(state.galleryTotal / Math.max(1, state.galleryLimit)));
    els.galleryPagination.classList.toggle("is-hidden", totalPages <= 1 && state.galleryPage === 0);
    els.galleryPageLabel.textContent = `第 ${state.galleryPage + 1} / ${totalPages} 页 · 共 ${state.galleryTotal} 条`;
    els.galleryPrev.disabled = state.galleryPage <= 0;
    els.galleryNext.disabled = state.galleryPage >= totalPages - 1;
    library.syncFloatingBars();
  }

  function stopGalleryPreviews() {
    if (!galleryPreviewSession) return;
    galleryPreviewSession.active = false;
    galleryPreviewSession.observer?.disconnect();
    galleryPreviewSession.queue.length = 0;
    galleryPreviewSession = null;
  }

  function currentGalleryPreview(session, card, item) {
    return session.active && galleryPreviewSession === session && state.view === "gallery"
      && card.isConnected && card.galleryMediaKey === imageMediaKey(item, "preview");
  }

  function galleryPreviewFailed(session, card, item, source = "") {
    if (!currentGalleryPreview(session, card, item)) return;
    if (source) { discardImageMediaSource(source); delete item.thumbnail_data_url; }
    card.galleryThumbnail = "";
    card.dataset.previewStatus = "error";
    const wrap = card.querySelector(".gallery-image-wrap");
    wrap.setAttribute("aria-busy", "false");
    wrap.querySelector("img")?.remove();
    wrap.querySelector(".gallery-image-pending")?.remove();
    if (wrap.querySelector(".gallery-preview-retry")) return;
    const retry = document.createElement("button");
    retry.type = "button"; retry.className = "quiet-button gallery-preview-retry";
    retry.textContent = "重试预览"; retry.setAttribute("aria-label", "预览加载失败，重新加载此图片");
    retry.addEventListener("click", event => {
      event.stopPropagation(); retry.remove();
      const pending = document.createElement("div"); pending.className = "gallery-image-pending";
      pending.append(window.ImageStudioImagePlaceholder.create()); wrap.prepend(pending);
      card.dataset.previewStatus = "pending"; wrap.setAttribute("aria-busy", "true");
      session.queue.unshift({ card, item }); pumpGalleryPreviews();
    });
    wrap.append(retry);
  }

  function paintGalleryPreview(session, card, item, source) {
    if (!currentGalleryPreview(session, card, item)) return;
    const wrap = card.querySelector(".gallery-image-wrap");
    let image = wrap.querySelector("img");
    if (!image) {
      image = document.createElement("img"); image.alt = item.prompt_preview || "";
      image.decoding = "async"; wrap.prepend(image);
    }
    const ready = () => {
      if (!currentGalleryPreview(session, card, item) || image.getAttribute("src") !== source) return;
      card.dataset.previewStatus = "ready"; card.galleryThumbnail = source;
      item.thumbnail_data_url = source;
      wrap.setAttribute("aria-busy", "false");
      wrap.querySelector(".gallery-image-pending")?.remove();
    };
    image.onload = ready;
    image.onerror = () => galleryPreviewFailed(session, card, item, source);
    if (image.getAttribute("src") !== source) image.src = source;
    if (image.complete && image.naturalWidth > 0) ready();
  }

  function pumpGalleryPreviews() {
    const session = galleryPreviewSession;
    if (!session?.active || state.view !== "gallery") return;
    // Old in-flight bridge calls cannot be aborted; discard their queued work
    // and keep a global bound so rapid paging cannot flood the connection.
    while (galleryPreviewActive < GALLERY_PREVIEW_CONCURRENCY && session.queue.length) {
      const { card, item } = session.queue.shift();
      if (!currentGalleryPreview(session, card, item)) continue;
      galleryPreviewActive++;
      void loadImageMedia(item, "preview").then(source => paintGalleryPreview(session, card, item, source))
        .catch(() => galleryPreviewFailed(session, card, item))
        .finally(() => { galleryPreviewActive--; pumpGalleryPreviews(); });
    }
  }

  function startGalleryPreviews() {
    stopGalleryPreviews();
    if (state.view !== "gallery") return;
    const session = { active: true, queue: [], observer: null };
    galleryPreviewSession = session;
    const items = new Map(state.galleryItems.map(item => [item.id, item]));
    const enqueue = card => {
      if (!session.active || card.dataset.previewStatus !== "pending") return;
      const item = items.get(card.dataset.galleryId);
      if (!item) return;
      card.dataset.previewStatus = "queued";
      session.observer?.unobserve(card);
      session.queue.push({ card, item });
    };
    if (typeof IntersectionObserver !== "undefined") session.observer = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) enqueue(entry.target);
      // Visible cards take priority over the near-viewport buffer.
      session.queue.sort((a, b) => {
        const distance = card => { const rect = card.getBoundingClientRect(); return Math.max(0, rect.top - innerHeight, -rect.bottom); };
        return distance(a.card) - distance(b.card);
      });
      pumpGalleryPreviews();
    }, { rootMargin: "240px" });
    els.galleryGrid.querySelectorAll("[data-gallery-id]").forEach(card => {
      const item = items.get(card.dataset.galleryId);
      card.galleryMediaKey = imageMediaKey(item, "preview");
      const source = item.thumbnail_data_url || getImageMedia(item, "preview");
      card.querySelector(".gallery-preview-retry")?.remove();
      if (source) { paintGalleryPreview(session, card, item, source); return; }
      const wrap = card.querySelector(".gallery-image-wrap");
      if (!wrap.querySelector(".gallery-image-pending")) {
        const pending = document.createElement("div"); pending.className = "gallery-image-pending";
        wrap.prepend(pending);
      }
      const pending = wrap.querySelector(".gallery-image-pending");
      if (!pending.firstChild) pending.append(window.ImageStudioImagePlaceholder.create());
      card.dataset.previewStatus = "pending"; wrap.setAttribute("aria-busy", "true");
      if (session.observer) session.observer.observe(card);
      else enqueue(card);
    });
    pumpGalleryPreviews();
  }

  function galleryFilters() {
    const filters = { query: els.gallerySearch.value, favorite: $("galleryFavorite").value, sort: gallerySort };
    for (const [id, key] of galleryFilterFields) {
      const selection = galleryFilterSelections.get(id);
      if (selection?.mode === "values") filters[key] = JSON.stringify(selection.values);
    }
    return filters;
  }

  function updateGalleryFilterOptions(select, entries) {
    // Facets describe all visible records. An absent category is removed from
    // the menu, while explicit saved selections retain their filtering intent.
    const unknown = ([value, label]) => !value || ["unknown", "unspecified"].includes(value) || /^(未知|未指定)/.test(label);
    entries = Array.from(new Map(entries.map(([value, label]) => [String(value), [String(value), label]])).values()).sort((left, right) => Number(unknown(left)) - Number(unknown(right)));
    const previous = Array.from(select.options);
    if (JSON.stringify(previous.map((option) => [option.value, option.label])) === JSON.stringify(entries)) return;
    const selection = galleryFilterSelections.get(select.id) || { mode: "all" };
    const all = selection.mode === "all";
    const selected = new Set(selection.values || []);
    select.replaceChildren(...entries.map(([value, label]) => new Option(label, value, all, all || selected.has(value))));
    window.ImageStudioSelect?.refresh(select);
  }

  function renderGallery(payload) {
    els.galleryGrid.querySelectorAll(".gallery-card.is-placeholder").forEach(card => card.remove());
    updateGalleryFilterOptions(els.galleryProvider, (payload.filters?.providers || []).map((item) => [item.id, item.name || item.id || "未指定服务商"]));
    updateGalleryFilterOptions(els.galleryMode, (payload.filters?.modes || []).map(value => [value, library.modeLabel(value)]));
    updateGalleryFilterOptions(els.gallerySource, (payload.filters?.sources || []).map(value => [value, sourceLabel(value)]));
    updateGalleryFilterOptions($("galleryEngine"), (payload.filters?.generation_engines || []).map(value => [value === "nai" ? "novelai" : value, library.engineLabel(value)]));
    els.galleryEmpty.classList.toggle("is-hidden", state.galleryItems.length > 0);
    const filters = galleryFilters();
    els.galleryEmpty.textContent = filters.query || filters.favorite || Object.keys(filters).length > 3 ? "没有符合当前筛选条件的图片。" : "画廊中还没有保留的生成记录。";
    const existing = new Map(Array.from(els.galleryGrid.children, (card) => [card.dataset.galleryId, card]));
    const wanted = new Set(state.galleryItems.map((item) => item.id));
    for (const [id, card] of existing) if (!wanted.has(id)) card.remove();
    // Unchanged records keep their decoded images, focus and hover state across refreshes.
    state.galleryItems.forEach((item, index) => {
      item.thumbnail_data_url ||= getImageMedia(item, "preview");
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
    updateGalleryPagination();
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
      data_url: (touchImageDisplay() ? item?._displayDataUrl : item?.data_url) || item?.thumbnail_data_url || (index === 0 ? fallbackThumbnail : ""),
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
        if (!nearby) { delete image.data_url; image._originalLoaded = false; delete image._displayDataUrl; delete image._displayEdge; }
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

  async function waitDetailDisplayIdle(frame, current) {
    while (current() && frame?.isConnected && (document.hidden || frame.dataset.detailSwipeState)) {
      await new Promise(resolve => setTimeout(resolve, 40));
    }
    return current();
  }

  async function loadDetailDisplay(detail, image, session = detailNavigationSession) {
    const frame = els.drawerBody.querySelector(".detail-image-frame");
    const edge = displayImageEdge(image);
    const current = () => isCurrentDetailImage(detail, image, session) && !mobileViewerSession?.active;
    const cached = getImageMedia(image, `display:${edge}`);
    if (cached) { image._displayDataUrl = cached; image._displayEdge = edge; return image; }
    if (image._displayDataUrl && image._displayEdge >= edge) return image;
    const epoch = session?.epoch;
    const source = await queueDetailRead(session, `display:${edge}:${image.id}`, async () => {
      if (!await waitDetailDisplayIdle(frame, current)) return "";
      return loadImageMedia(image, `display:${edge}`);
    }, current, true);
    if (!source || !current() || session.epoch !== epoch) return null;
    image._displayDataUrl = source; image._displayEdge = edge;
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
    return { src: src || "", cursor };
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
    const target = { src: image.thumbnail_data_url || image.data_url || "", cursor, detail, index };
    // Identity and media readiness are separate: navigation consumes the target
    // immediately; the drag pane and speculative warmer can await its preview.
    target.previewReady = (async () => {
      const wanted = () => {
        const currentId = state.detailData?.images?.[state.detailImageIndex]?.id;
        return [currentId, detailNeighborCursor(-1, session)?.image_id, detailNeighborCursor(1, session)?.image_id]
          .some(id => id && String(id) === String(cursor.image_id));
      };
      const src = target.src || await loadDetailPreview(detail, image, session, wanted);
      if (!src || !currentDetailSession(session) || epoch !== session.epoch) return;
      await decodeDisplayImage(src);
      if (currentDetailSession(session) && epoch === session.epoch) target.src = src;
    })();
    target.previewReady.catch(() => {});
    return target;
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
      await Promise.all([-1, 1].map(async (direction) => {
        try { const target = await prepareDetailTarget(detailNeighborCursor(direction, session), session); await target?.previewReady; }
        catch { /* A preview failure must not disable the target's navigation. */ }
      }));
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
      const image = new Image(); image.className = "detail-image"; image.decoding = "async"; image.src = source;
      const entry = { ready: false, promise: null, bytes: 0 };
      entry.promise = image.decode().then(() => {
        if (!image.naturalWidth || !image.naturalHeight) throw new Error("图片内容无法解码。");
        entry.ready = true;
        if (decodedDisplayImages.get(source) === entry) {
          entry.bytes = image.naturalWidth * image.naturalHeight * 4;
          decodedDisplayBytes += entry.bytes;
          while (decodedDisplayBytes > 32 * 1024 * 1024 || decodedDisplayImages.size > 8) {
            const first = decodedDisplayImages.keys().next().value;
            decodedDisplayBytes -= decodedDisplayImages.get(first).bytes;
            decodedDisplayImages.delete(first);
          }
        }
        return image;
      }).catch((error) => {
        if (decodedDisplayImages.get(source) === entry) decodedDisplayImages.delete(source);
        discardImageMediaSource(source);
        throw error;
      });
      decodedDisplayImages.set(source, entry);
      while (decodedDisplayImages.size > 8) {
        const first = decodedDisplayImages.keys().next().value;
        decodedDisplayBytes -= decodedDisplayImages.get(first).bytes;
        decodedDisplayImages.delete(first);
      }
    }
    return decodedDisplayImages.get(source).promise;
  }

  function forgetDecodedImage(source) {
    const entry = decodedDisplayImages.get(source);
    if (entry) { decodedDisplayBytes -= entry.bytes; decodedDisplayImages.delete(source); }
  }

  function showDetailImagePending(frame, reset = false) {
    let pending = frame.querySelector(".detail-image-pending");
    if (!pending) {
      pending = document.createElement("div"); pending.className = "detail-image-pending";
      pending.setAttribute("role", "status"); frame.appendChild(pending);
    }
    if (!pending.querySelector(".image-studio-image-placeholder") && (!pending.textContent || reset)) {
      const text = document.createElement("span"); text.className = "detail-image-loading-text";
      text.textContent = "正在读取图片…";
      pending.replaceChildren(window.ImageStudioImagePlaceholder.create(), text);
    }
  }

  async function paintDetailImage(frame, item, index) {
    const revision = ++detailImagePaintRevision;
    if (!frame) return false;
    const image = frame.querySelector("[data-detail-image]");
    const target = item?.data_url || "";
    const preview = decodedDisplayImages.get(target)?.ready ? target : item?.thumbnail_data_url || target;
    const imageKey = String(item?.id || `${state.detailId}:${index}`);
    const changed = image.dataset.imageKey !== imageKey;
    image.dataset.imageKey = imageKey;
    image.dataset.detailImage = String(index); image.alt = `生成结果 ${index + 1}`;
    if (changed) {
      // Never leave the previous group's foreground under a new cursor. A
      // missing/undecoded source gets a navigable placeholder instead.
      if (!preview || !decodedDisplayImages.get(preview)?.ready) image.removeAttribute("src");
      frame.setAttribute("aria-busy", "true");
    }
    if (!image.getAttribute("src")) showDetailImagePending(frame, changed || !!item?.data_url);
    if (!item?.data_url) { frame.setAttribute("aria-busy", "true"); return false; }
    const current = () => frame.isConnected && image.dataset.imageKey === imageKey && frame.dataset.generationId === String(state.detailId) && revision === detailImagePaintRevision && (!mobileViewerSession?.active || mobileViewerSession.preparingDetail);
    const publish = async (source) => {
      if (!current()) return false;
      const previousSource = image.getAttribute("src");
      if (previousSource && previousSource !== source && touchImageDisplay() && !mobileViewerSession?.preparingDetail) {
        if (!await waitDetailDisplayIdle(frame, current)) return false;
      }
      if (previousSource !== source) image.src = source;
      // A decoded candidate does not guarantee Safari has selected it on the mounted image.
      try { await image.decode(); }
      catch (error) {
        if (current() && image.getAttribute("src") === source) {
          if (previousSource && !changed) image.src = previousSource;
          else image.removeAttribute("src");
        }
        throw error;
      }
      if (!current()) return false;
      image.dataset.detailImage = String(index); image.alt = `生成结果 ${index + 1}`;
      frame.querySelector(".detail-image-pending")?.remove(); frame.removeAttribute("aria-busy");
      transitionDetailBackdrop(frame); return true;
    };
    if (!image.getAttribute("src")) frame.setAttribute("aria-busy", "true");
    try {
      if (!decodedDisplayImages.get(preview)?.ready) await decodeDisplayImage(preview);
      if (!await publish(preview)) return false;
      if (target !== preview) void (async () => {
        if (touchImageDisplay() && !await waitDetailDisplayIdle(frame, current)) return;
        await decodeDisplayImage(target); await publish(target);
      })().catch(() => {});
      return true;
    } catch (error) {
      if (target !== preview) {
        try { await decodeDisplayImage(target); return await publish(target); } catch { /* Keep the selected cursor and allow navigation when both sources fail. */ }
      }
      if (current()) {
        frame.removeAttribute("aria-busy"); showNotice(errorMessage(error, "图片暂时无法显示"), "error");
        detailImageLoadFailed();
      }
      return false;
    }
  }

  function detailImageLoadFailed() {
    const frame = els.drawerBody.querySelector(".detail-image-frame");
    if (!frame || frame.querySelector("[data-detail-image]")?.getAttribute("src")) return;
    frame.removeAttribute("aria-busy");
    let pending = frame.querySelector(".detail-image-pending");
    if (!pending) { pending = document.createElement("div"); pending.className = "detail-image-pending"; frame.append(pending); }
    pending.textContent = "图片暂时无法显示，可继续切换";
  }

  function createDetailImageFrame(generationId) {
    const frame = document.createElement("div"); frame.className = "detail-image-frame"; frame.dataset.generationId = generationId;
    frame.innerHTML = `<div class="detail-image-background" aria-hidden="true"><img class="detail-image-backdrop" ${detailBackdropSource ? `src="${escape(detailBackdropSource)}"` : ""} alt="" /></div><img class="detail-image" decoding="async" alt="生成结果" data-detail-image="0" /><button class="detail-carousel-nav is-previous" data-detail-nav="-1" type="button" aria-label="查看上一张图片"><span aria-hidden="true">‹</span></button><button class="detail-carousel-nav is-next" data-detail-nav="1" type="button" aria-label="查看下一张图片"><span aria-hidden="true">›</span></button>`;
    showDetailImagePending(frame);
    const current = () => frame.isConnected && frame.dataset.generationId === String(state.detailId);
    window.ImageStudioDetailSwipe.bind(frame, {
      getNeighbor: (direction) => {
        if (!current()) return null;
        return cachedDetailNeighbor(direction);
      },
      prepareNeighbor: (direction) => current() ? prepareDetailNeighbor(direction) : null,
      navigate: (direction, target) => current() ? navigateDetail(direction, target) : false,
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
        return `<button class="detail-filmstrip-thumb" data-detail-dot="${index}" type="button" aria-label="查看本次生成的第 ${index + 1} 张图片"><span class="detail-filmstrip-preview"><span class="detail-filmstrip-placeholder" aria-hidden="true" ${preview ? "hidden" : ""}>${index + 1}</span>${preview ? `<img src="${escape(preview)}" alt="" draggable="false" loading="lazy" decoding="async" />` : ""}</span></button>`;
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
    if (detail.lightweight) state.detailAssetsLoaded = detailDisplayReady(detail.images[imageIndex]);
    if (state.detailAssetsLoaded) state.detailRequestedImageIndex = imageIndex;
    const refs = Array.isArray(detail.references) ? detail.references : [];
    const totalBytes = images.reduce((sum, item) => sum + Number(item.size_bytes || 0), 0);
    const externalTimeLabels = { nai_filename: "NAI 保存时间", metadata: "元数据创建时间", btime: "文件创建时间", birthtime: "文件创建时间", mtime: "文件修改时间" };
    const sortTimeLabel = detail.is_external ? externalTimeLabels[detail.time_source] || "排序时间" : "记录时间";
    els.detailDate.textContent = `${detail.is_external ? `${sortTimeLabel}：` : ""}${formatDate(detail.created_at)}`;
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
      : `${library.detailMetadataMarkup(detail, currentImage)}<div class="detail-block"><h3>信息</h3><pre>${escape(JSON.stringify({ 服务商: detail.provider_name, 模型: detail.model, 模式: library.modeLabel(detail.mode), 生图来源: library.engineLabel(detail.generation_engine), 来源: sourceLabel(detail.source), 调用来源身份: sourceIdentity, [sortTimeLabel]: formatDate(detail.created_at), 图片数量: images.length, 文件大小: formatBytes(totalBytes), 耗时毫秒: detail.elapsed_ms }, null, 2))}</pre></div><div class="detail-block"><h3>参考图</h3><div class="detail-references" data-detail-references>${detailReferenceMarkup(refs)}</div></div>${library.detailWarningsMarkup(detail, currentImage)}`;
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
    els.drawerBody.querySelector("[data-output-reference]")?.addEventListener("click", () => void useGalleryImageAsReference(currentImage));
    els.drawerBody.scrollTop = scrollTop;
    if (detail.lightweight && !mobileViewerSession?.active) void loadDetailAssets(detail.id, detail, fallbackThumbnail);
    // Preload from the cursor even if the selected image is not yet available.
    void warmDetailNeighbors();
    return paintDetailImage(imageFrame, currentImage, imageIndex);
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
        detailImageLoadFailed();
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
        if (current() && (session.epoch !== loadEpoch
          || key === "_displayTask" && !image._displayError && !detailDisplayReady(image))) void loadDetailAssets(id, summary, fallbackThumbnail);
      };
      const paint = () => {
        if (!current()) return;
        state.detailAssetsLoaded = detailDisplayReady(image);
        library.updateDetailActions(summary);
        void paintDetailImage(els.drawerBody.querySelector(".detail-image-frame"), detailDisplayImages(summary, fallbackThumbnail)[state.detailImageIndex], state.detailImageIndex);
        if (state.imagePreviewContext?.type === "detail" && state.imagePreviewContext.generationId === id && state.imagePreviewContext.imageIndex === state.detailImageIndex && !els.imagePreview.classList.contains("is-hidden")) {
          Object.assign(state.imagePreviewItems[state.imagePreviewIndex], image);
          renderImagePreview();
        }
      };
      if (!image.thumbnail_data_url && !image._previewTask) image._previewTask = loadDetailPreview(summary, image, session, current).then(paint).catch(() => {
        if (current() && session.epoch === loadEpoch) detailImageLoadFailed();
      }).finally(() => settled("_previewTask"));
      const mobileDisplay = touchImageDisplay();
      const mediaTask = mobileDisplay ? "_displayTask" : "_originalTask";
      const mediaError = mobileDisplay ? "_displayError" : "_originalError";
      if (!detailDisplayReady(image) && !image[mediaError] && !image[mediaTask]) image[mediaTask] = (mobileDisplay ? loadDetailDisplay : loadDetailOriginal)(summary, image, session).then(paint).catch(error => {
        if (!current() || session.epoch !== loadEpoch) return;
        detailImageLoadFailed();
        image[mediaError] = errorMessage(error, mobileDisplay ? "图片显示加载失败" : "原图加载失败"); showNotice(image[mediaError], "error");
      }).finally(() => settled(mediaTask));
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
        referenceButton.addEventListener("click", () => void useGalleryImageAsReference(currentImage));
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
    els.detailDrawer.classList.add("is-open"); els.detailDrawer.setAttribute("aria-hidden", "false"); syncPageScrollLock(); if (options.focus !== false) els.detailDrawer.focus({ preventScroll: true });
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
      void renderDetail(detail, state.detailFallbackThumbnail);
      if (crossGroup && currentDetailSession(session) && String(state.detailId) === String(target.cursor.generation_id)) scheduleDetailAssets(state.detailId, detail, state.detailFallbackThumbnail);
      return true;
    } catch (error) {
      if (currentDetailSession(session) && revision === detailRequestRevision) showNotice(`图片切换失败：${errorMessage(error, "目标图片暂时无法读取")}`, "error");
      return false;
    } finally { if (currentDetailSession(session)) state.detailNavigating = false; }
  }

  function closeDetail() { library.clearDetailParameterLayout(); stopDetailNavigation(); window.ImageStudioBackdrop.dispose(els.drawerBody.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)")); detailImagePaintRevision++; mobileDetailSyncRevision++; decodedDisplayImages.clear(); decodedDisplayBytes = 0; detailBackdropSource = ""; detailRequestRevision += 1; if (mobileImageViewer) { suppressMobileDetailSync = true; mobileImageViewer.close(); } state.detailId = ""; state.detailData = null; state.detailFallbackThumbnail = ""; state.detailAssetsLoaded = false; state.detailNavigating = false; state.detailImageIndex = 0; state.detailRequestedImageIndex = 0; closeImagePreview(); els.detailDrawer.classList.remove("is-open"); els.detailDrawer.setAttribute("aria-hidden", "true"); syncPageScrollLock(); }

  function isMobileDetailPreview(context) {
    return context?.type === "detail" && window.ImageStudioDetailSwipe.usesTouchInteraction() && typeof window.PhotoSwipe === "function";
  }

  function mobileSourceForSequenceItem(sequenceItem, fallbackDataUrl, context, knownImages, galleryById) {
    const cachedPreview = getImageMedia(sequenceItem, "preview");
    if (cachedPreview) return { src: cachedPreview, detail: "preview" };
    const sameRecord = String(state.detailId) === String(sequenceItem.generation_id);
    const known = sameRecord ? knownImages.get(String(sequenceItem.image_id)) || knownImages.get(`index:${sequenceItem.image_index}`) : null;
    if (known?.thumbnail_data_url) return { src: known.thumbnail_data_url, detail: "preview" };
    if (known?._displayDataUrl) return { src: known._displayDataUrl, detail: "display" };
    if (String(sequenceItem.generation_id) === String(context.generationId) && sequenceItem.image_index === context.imageIndex && fallbackDataUrl) return { src: fallbackDataUrl, detail: "display" };
    const card = sequenceItem.image_index === 0 ? galleryById.get(String(sequenceItem.generation_id)) : null;
    if (card?.thumbnail_data_url) return { src: card.thumbnail_data_url, detail: "preview" };
    return { src: EMPTY_MOBILE_IMAGE, detail: "" };
  }

  function isCurrentMobileSession(session, index, item) {
    return !!session?.active && !session.viewer.isDestroying && mobileViewerSession === session && mobileImageViewer === session.viewer && (!item || session.items[index] === item);
  }

  function mobileViewerBusy(session) {
    const viewer = session.viewer;
    return document.hidden || session.inputSuspended || session.pointerIds.size > 0 || session.touchCount > 0 || viewer.opener.isOpening
      || viewer.gestures.isDragging || viewer.gestures.isZooming || viewer.mainScroll.isShifted()
      || viewer.animations.activeAnimations.some((animation) => animation.props.isMainScroll || animation.props.isPan);
  }

  function scheduleMobileWork(session) {
    if (!isCurrentMobileSession(session) || session.workTimer || !session.workQueue.size) return;
    session.workTimer = window.setTimeout(() => {
      session.workTimer = 0;
      if (!isCurrentMobileSession(session)) return;
      if (mobileViewerBusy(session)) { scheduleMobileWork(session); return; }
      // Start only one stage per turn. The next touch gets a chance to pause
      // the remaining work instead of inheriting a burst of promise callbacks.
      const [key, job] = session.workQueue.entries().next().value;
      session.workQueue.delete(key);
      Promise.resolve().then(job.run).then(job.resolve, job.reject);
      scheduleMobileWork(session);
    }, 32);
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
    if (!isCurrentMobileSession(session) || session.resettingGesture) return;
    const original = event.originalEvent;
    // Browser cancellation is not a released drag. Cancel before PhotoSwipe
    // finishes its pointerUp dispatch and attempts inertia or vertical close.
    if (!down && /cancel$/.test(original?.type || "")) { resetMobileGesture(session); return; }
    // A fresh primary touch proves the previous touch sequence has ended,
    // even when iOS's image callout omitted its terminal/contextmenu events.
    const freshTouch = down && (original?.pointerType === "touch" && original.isPrimary === true
      || original?.type === "touchstart" && original.touches?.length === 1);
    if (freshTouch && (session.pointerIds.size || session.touchCount)) resetMobileGesture(session);
    if (down) session.inputSuspended = false;
    if (original?.pointerId !== undefined) down ? session.pointerIds.add(original.pointerId) : session.pointerIds.delete(original.pointerId);
    else session.touchCount = original?.touches?.length ?? (down ? 1 : 0);
    if (down) {
      session.entryAnimation?.cancel();
      window.clearTimeout(session.workTimer); session.workTimer = 0;
      window.ImageStudioViewerBackdrop.pause(session.backdrop);
    } else {
      updateMobileViewerFeedback(session); scheduleMobileWork(session);
    }
  }

  function resetMobileGesture(session, suspend = false) {
    if (!isCurrentMobileSession(session) || session.resettingGesture) return;
    const viewer = session.viewer, gestures = viewer.gestures;
    const index = viewer.currIndex;
    session.resettingGesture = true;
    session.inputSuspended = suspend;
    window.clearTimeout(session.workTimer); session.workTimer = 0;
    session.entryAnimation?.cancel();
    window.ImageStudioViewerBackdrop.pause(session.backdrop);
    try {
      // Compatibility adapter for the bundled PhotoSwipe 5.4.4 gesture engine.
      // Its cancel event removes internal contacts, but normally also releases
      // a drag with inertia. Suppress that release: a system menu must not
      // accidentally flip a page, close the viewer or turn into a tap.
      gestures.isDragging = false; gestures.isZooming = false;
      gestures.dragAxis = null;
      gestures.velocity.x = gestures.velocity.y = 0;
      for (const pointerId of session.pointerIds) {
        gestures.onPointerUp({ type: "pointercancel", pointerId, target: viewer.scrollWrap });
      }
      if (session.touchCount) gestures.onPointerUp({ type: "touchcancel", touches: [], target: viewer.scrollWrap });
      gestures.isMultitouch = false;
      viewer.animations.stopAll();
      if (viewer.mainScroll.isShifted()) viewer.goTo(index);
      const slide = viewer.currSlide;
      if (slide) {
        const zoom = Math.max(slide.zoomLevels.min, Math.min(slide.zoomLevels.max, slide.currZoomLevel));
        if (zoom !== slide.currZoomLevel) slide.zoomTo(zoom, undefined, 0);
        slide.panTo(slide.pan.x, slide.pan.y);
      }
      viewer.applyBgOpacity(1);
    } finally {
      session.pointerIds.clear(); session.touchCount = 0;
      session.resettingGesture = false;
    }
    if (!suspend) { updateMobileViewerFeedback(session); scheduleMobileWork(session); }
  }

  function bindMobileGestureInterruption(session) {
    const viewer = session.viewer;
    const interrupt = () => resetMobileGesture(session, true);
    const resume = () => {
      if (!isCurrentMobileSession(session)) return;
      session.inputSuspended = false;
      updateMobileViewerFeedback(session); scheduleMobileWork(session);
    };
    // Leave the native menu enabled. These listeners are owned by PhotoSwipe
    // and removed with its normal close/destroy lifecycle.
    viewer.events.add(viewer.scrollWrap, "contextmenu", interrupt);
    viewer.events.add(window, "blur pagehide", interrupt);
    viewer.events.add(window, "focus pageshow", resume);
    viewer.events.add(document, "visibilitychange", () => { if (document.hidden) interrupt(); else resume(); });
    viewer.events.add(window, "pointercancel touchcancel", event => {
      const handledInside = event.target instanceof Node && viewer.scrollWrap.contains(event.target);
      if (!handledInside && (session.pointerIds.has(event.pointerId) || session.touchCount)) resetMobileGesture(session);
    });
  }

  function mobileWantsOriginal(session, index = session.viewer.currIndex) {
    const slide = session.viewer.currSlide;
    return index === session.viewer.currIndex && slide?.currZoomLevel > slide?.zoomLevels.initial * 1.01;
  }

  function mobileDisplaySource(item, session, index) {
    return mobileWantsOriginal(session, index) && item.originalSrc || item.displaySrc || item.previewSrc || EMPTY_MOBILE_IMAGE;
  }

  function updateMobileImageSource(index, payload, detail, session = mobileViewerSession) {
    const item = session?.items[index];
    if (!isCurrentMobileSession(session, index, item) || !item || !payload?.data_url) return;
    if (payload.id && String(payload.id) !== String(item.image_id)) return;
    if (detail === "original" && session.viewer.currIndex !== index) return;
    if (detail === "original") { item.originalSrc = payload.data_url; session.originalIndices.add(index); }
    else if (detail === "display") { item.displaySrc = payload.data_url; item.displayEdge = payload.max_edge; session.displayIndices.add(index); }
    else item.previewSrc = payload.data_url;
    if (detail === "original") { item.originalError = ""; item.previewError = ""; item.originalRetryCount = 0; }
    else if (detail === "display") item.displayError = "";
    else item.previewError = "";
    const source = mobileDisplaySource(item, session, index);
    item.src = source; item.msrc = item.previewSrc || item.displaySrc || source;
    item.loadedDetail = source === item.originalSrc ? "original" : source === item.displaySrc ? "display" : "preview";
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
      if (content.element?.tagName === "IMG") content.element.decoding = "async";
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
    const edge = detail === "display" ? displayImageEdge(item) : 0;
    const loads = session.loads;
    if (!loads.has(key)) {
      const relevant = () => isCurrentMobileSession(session, index, item)
        && Math.abs(session.viewer.currIndex - index) <= (detail === "preview" ? 1 : 0)
        && (detail !== "original" || mobileWantsOriginal(session, index));
      const errorKey = `${detail}Error`;
      item[errorKey] = "";
      let preparedOriginal = "";
      let encodedOriginal = "";
      const pending = queueMobileWork(session, `load:${key}`, async () => {
        if (!relevant()) return;
        const available = detail === "original" ? item.originalSrc : detail === "display" ? (item.displayEdge >= edge ? item.displaySrc : "") : item.previewSrc;
        const known = detail === "original" && String(state.detailId) === String(item.generation_id)
          ? state.detailData?.images?.find((image) => String(image.id) === String(item.image_id)) : null;
        const knownOriginal = known?._originalLoaded || (!state.detailData?.lightweight && state.detailAssetsLoaded) ? known?.data_url : "";
        const mediaDetail = detail === "display" ? `display:${edge}` : detail;
        const source = available || knownOriginal || await loadImageMedia(item, mediaDetail);
        if (detail === "original" && source?.startsWith("data:")) encodedOriginal = source;
        const payload = { id: item.image_id, data_url: source, max_edge: edge };
        if (source?.startsWith("data:")) cacheImageMedia(item, mediaDetail, source);
        if (!relevant()) return;
        if (!payload?.data_url) throw new Error("图片接口没有返回可用内容。");
        // Original bytes cross the host's authenticated JSON bridge only on
        // zoom. Convert off-thread so DOM and decode-cache keys stay small.
        if (detail === "original" && !payload.data_url.startsWith("blob:")) {
          payload.data_url = await queueMobileWork(session, `object:${key}`, () => relevant() ? session.mediaObjects.source(key, source) : "");
          preparedOriginal = payload.data_url;
          if (!relevant()) { session.mediaObjects.drop(key); return; }
          if (!payload.data_url) throw new Error("原图显示准备失败，请重试。");
        }
        // A response or decode may finish during a later gesture; gate both stages separately.
        const decoded = await queueMobileWork(session, `decode:${key}`, () => relevant() ? decodeDisplayImage(payload.data_url) : undefined);
        if (!decoded || !relevant()) return;
        await queueMobileWork(session, `paint:${key}`, () => { if (relevant()) updateMobileImageSource(index, payload, detail, session); });
        return payload;
      }).catch(error => {
        // A failed Blob decode invalidates the encoded source too; retaining
        // its data URL would make retry reuse the same broken HTTP 200 body.
        if (encodedOriginal) discardImageMediaSource(encodedOriginal);
        if (!relevant()) return;
        item[errorKey] = errorMessage(error, detail === "original" ? "原图加载失败" : "预览加载失败");
        throw error;
      }).finally(() => {
        if (preparedOriginal && item.originalSrc !== preparedOriginal && loads.get(key) === pending) {
          forgetDecodedImage(preparedOriginal);
          session.mediaObjects.drop(key);
        }
        if (loads.get(key) === pending) loads.delete(key);
        updateMobileViewerFeedback(session);
        if (relevant() && !item[errorKey] && detail !== "preview") {
          const missing = detail === "original" ? !item.originalSrc : !item.displaySrc || item.displayEdge < displayImageEdge(item);
          if (missing) syncMobileResolution(session);
        }
      });
      loads.set(key, pending);
      updateMobileViewerFeedback(session);
    }
    await loads.get(key);
  }

  function pruneMobileOriginals(currentIndex, session = mobileViewerSession) {
    if (!isCurrentMobileSession(session)) return;
    session.originalIndices.forEach((index) => {
      if (Math.abs(index - currentIndex) <= 1) return;
      session.originalIndices.delete(index);
      const item = session.items[index];
      forgetDecodedImage(item.originalSrc);
      session.mediaObjects.drop(`${item.image_id}:original`);
      item.originalSrc = "";
      item.src = item.displaySrc || item.previewSrc || EMPTY_MOBILE_IMAGE;
      item.msrc = item.previewSrc || item.src;
      item.loadedDetail = item.displaySrc ? "display" : item.previewSrc ? "preview" : "";
      const cached = session.viewer.contentLoader?.getContentByIndex(index);
      if (cached && !cached.hasSlide && !cached.isAttached) { session.viewer.contentLoader.removeByIndex(index); cached.destroy(); }
    });
    // Session cursors must not turn into an unbounded second display cache.
    // Keep current neighbors attached; farther images return to their small
    // previews and can reuse the byte-budgeted shared cache on the next visit.
    session.displayIndices.forEach(index => {
      if (Math.abs(index - currentIndex) <= 1) return;
      session.displayIndices.delete(index);
      const item = session.items[index];
      item.displaySrc = ""; item.displayEdge = 0;
      item.src = item.previewSrc || EMPTY_MOBILE_IMAGE;
      item.msrc = item.src; item.loadedDetail = item.previewSrc ? "preview" : "";
      const cached = session.viewer.contentLoader?.getContentByIndex(index);
      if (cached && !cached.hasSlide && !cached.isAttached) { session.viewer.contentLoader.removeByIndex(index); cached.destroy(); }
    });
  }

  function updateMobileViewerFeedback(session = mobileViewerSession) {
    if (!isCurrentMobileSession(session)) return;
    void queueMobileWork(session, "feedback", () => applyMobileViewerFeedback(session));
  }

  function restoreMobileViewerBackground(session) {
    if (!isCurrentMobileSession(session)) return;
    const viewer = session.viewer;
    if (viewer.gestures.isZooming || (viewer.gestures.isDragging && viewer.gestures.dragAxis === "y")) return;
    // A new horizontal gesture can interrupt vertical-dismissal rebound before
    // it restores the background. PhotoSwipe's rebound only writes opacity
    // while it is below 1, so this also prevents its later frames dimming it
    // again without interrupting either the slide or the pan animation.
    if (viewer.bgOpacity < 1) viewer.applyBgOpacity(1);
  }

  function applyMobileViewerFeedback(session) {
    if (!isCurrentMobileSession(session)) return;
    // Feedback runs after the gesture and its rebound settle, including taps
    // and cancelled vertical drags that never change the current image.
    restoreMobileViewerBackground(session);
    const item = session.items[session.viewer.currIndex];
    const src = item?.previewSrc || "";
    if (session.backdrop) void window.ImageStudioViewerBackdrop.transition(session.backdrop, src);
    const content = session.viewer.currSlide?.content;
    const loading = item && (item.retrying || item.retryTimer
      || session.loads.has(`${item.image_id}:preview`) || session.loads.has(`${item.image_id}:original`) || session.loads.has(`${item.image_id}:display`)
      || content?.isLoading());
    const hasImage = !!(item?.previewSrc || item?.displaySrc || item?.originalSrc) && !content?.isError();
    // A failed placeholder or preview is not a terminal failure while another
    // source, decode, paint or scheduled retry can still supply this image.
    const error = !loading && (content?.isError() && item?.displayError
      || (mobileWantsOriginal(session) && !item?.originalSrc ? item?.originalError : !item?.displaySrc && item?.displayError)
      || !hasImage && item?.previewError);
    if (session.status) {
      session.status.hidden = hasImage && !error;
      session.status.dataset.state = error ? "error" : "loading";
      session.status.querySelector("span").textContent = error
        ? hasImage ? "高清图片加载失败，当前显示预览。" : "图片暂时无法加载。"
        : "正在读取图片…";
      session.status.querySelector("button").hidden = !error;
      session.status.querySelector("button").disabled = !!item?.retrying;
    }
  }

  async function retryMobileImage(session = mobileViewerSession, index = session?.viewer.currIndex) {
    if (!isCurrentMobileSession(session) || !session.items[index] || session.items[index].retrying) return;
    const item = session.items[index]; item.retrying = true;
    item.originalError = ""; item.previewError = ""; item.displayError = "";
    if (item.retryTimer) { window.clearTimeout(item.retryTimer); session.retryTimers.delete(item.retryTimer); item.retryTimer = 0; }
    updateMobileViewerFeedback(session);
    const content = session.viewer.contentLoader?.getContentByIndex(index);
    if (content?.isError() || content?.element?.tagName === "DIV") {
      item.originalSrc = ""; item.displaySrc = ""; item.previewSrc = ""; item.src = EMPTY_MOBILE_IMAGE;
    }
    try {
      // Neither source should delay starting the other during recovery.
      await Promise.allSettled([loadMobileImage(index, "preview", session), loadMobileImage(index, mobileWantsOriginal(session, index) ? "original" : "display", session)]);
    } finally { item.retrying = false; updateMobileViewerFeedback(session); }
  }

  function warmMobileImages(index, session = mobileViewerSession) {
    if (!isCurrentMobileSession(session)) return;
    void queueMobileWork(session, "prune", () => pruneMobileOriginals(session.viewer.currIndex, session));
    updateMobileViewerFeedback(session);
    for (const neighbor of [index - 1, index, index + 1]) {
      if (neighbor >= 0 && neighbor < session.items.length) void loadMobileImage(neighbor, "preview", session).catch(() => {});
    }
    const quality = mobileWantsOriginal(session, index) ? "original" : "display";
    const selected = session.items[index];
    if (quality === "original" ? selected?.originalSrc : selected?.displaySrc && selected.displayEdge >= displayImageEdge(selected)) return;
    void loadMobileImage(index, quality, session).catch(() => {
      if (!isCurrentMobileSession(session) || session.viewer.currIndex !== index) return;
      const item = session.items[index];
      if (!item.originalRetryCount) {
        item.originalRetryCount = 1;
        const timer = window.setTimeout(() => {
          session.retryTimers.delete(timer); item.retryTimer = 0;
          if (isCurrentMobileSession(session) && session.viewer.currIndex === index) void retryMobileImage(session, index);
        }, 650);
        item.retryTimer = timer;
        session.retryTimers.add(timer);
      }
      updateMobileViewerFeedback(session);
    });
  }

  function syncMobileResolution(session) {
    if (!isCurrentMobileSession(session)) return;
    void queueMobileWork(session, "resolution", () => {
      const index = session.viewer.currIndex, item = session.items[index];
      if (!item) return;
      const source = mobileDisplaySource(item, session, index);
      if (source !== EMPTY_MOBILE_IMAGE && source !== item.src) {
        const detail = source === item.originalSrc ? "original" : source === item.displaySrc ? "display" : "preview";
        updateMobileImageSource(index, { id: item.image_id, data_url: source, max_edge: item.displayEdge }, detail, session);
      }
      warmMobileImages(index, session);
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
    if (!item || item.allowed_actions?.download === false) return;
    try {
      const client = await bridge();
      await client.download(`gallery/download/${item.image_id}`, {}, item.download_filename);
    } catch (error) {
      showNotice(errorMessage(error, "图片下载失败"), "error");
    }
  }

  function toggleMobileImageControls() {
    const session = mobileViewerSession;
    if (!isCurrentMobileSession(session)) return;
    const visible = session.viewer.element.classList.toggle("image-studio-controls-visible");
    session.filmstrip?.setVisible(visible);
  }

  function createMobileFilmstrip(session) {
    const viewer = session.viewer;
    const strip = document.createElement("div");
    strip.className = "detail-filmstrip image-studio-viewer-filmstrip";
    strip.setAttribute("role", "group"); strip.setAttribute("aria-label", "本组生成图片缩略图");
    strip.setAttribute("aria-hidden", "true"); strip.inert = true; strip.hidden = true;
    viewer.element.appendChild(strip);
    const groups = new Map();
    session.items.forEach((item, index) => {
      const id = String(item.generation_id);
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(index);
    });
    let visible = false, generation = "", revision = 0, frame = 0, active = 0;
    let observer = null, queue = [];
    const currentGroup = () => String(session.items[viewer.currIndex]?.generation_id || "");
    const current = token => visible && token === revision && isCurrentMobileSession(session) && generation === currentGroup();

    function paint(button, source, token) {
      if (!source || !current(token) || !button.isConnected) return;
      const mount = button.querySelector(".detail-filmstrip-preview");
      if (mount.querySelector("img")?.getAttribute("src") === source) return;
      const image = document.createElement("img");
      image.alt = ""; image.draggable = false; image.decoding = "async";
      image.onload = () => { if (current(token) && image.parentNode === mount) mount.querySelector(".detail-filmstrip-placeholder").hidden = true; };
      image.onerror = () => {
        if (!current(token) || image.parentNode !== mount) return;
        discardImageMediaSource(source); image.remove(); mount.querySelector(".detail-filmstrip-placeholder").hidden = false;
      };
      image.src = source; mount.querySelector("img")?.remove(); mount.append(image);
      if (image.complete && image.naturalWidth > 0) mount.querySelector(".detail-filmstrip-placeholder").hidden = true;
    }

    function pump() {
      if (!visible || !isCurrentMobileSession(session)) return;
      while (active < 2 && queue.length) {
        const { button, index, token } = queue.shift();
        if (!current(token) || !button.isConnected) continue;
        active++;
        // Filmstrip reads populate the shared preview cache only. They must not
        // promote a far-away PhotoSwipe slide or touch the main blur compositor.
        void loadImageMedia(session.items[index], "preview").then(source => paint(button, source, token))
          .catch(() => { if (current(token) && button.isConnected) delete button.dataset.previewQueued; })
          .finally(() => { active--; pump(); });
      }
    }

    function load(button) {
      const index = Number(button.dataset.viewerIndex), item = session.items[index];
      if (!item || !current(revision)) return;
      const source = item.previewSrc || getImageMedia(item, "preview");
      if (source) { paint(button, source, revision); return; }
      if (button.dataset.previewQueued) return;
      button.dataset.previewQueued = "true";
      queue.push({ button, index, token: revision }); pump();
    }

    function render() {
      if (!visible || !isCurrentMobileSession(session)) return;
      const id = currentGroup(), indices = groups.get(id) || [];
      strip.hidden = indices.length <= 1;
      if (strip.hidden) return;
      if (generation !== id) {
        observer?.disconnect(); queue = []; revision++; generation = id;
        strip.dataset.generationId = id;
        strip.innerHTML = `<div class="detail-filmstrip-track">${indices.map(index => `<button class="detail-filmstrip-thumb" data-viewer-index="${index}" type="button" aria-label="查看本组第 ${Number(session.items[index].image_index) + 1} 张图片"><span class="detail-filmstrip-preview"><span class="detail-filmstrip-placeholder" aria-hidden="true">${Number(session.items[index].image_index) + 1}</span></span></button>`).join("")}</div>`;
      }
      delete strip.dataset.groupPending;
      strip.inert = false; strip.setAttribute("aria-busy", "false");
      const buttons = [...strip.querySelectorAll("[data-viewer-index]")];
      buttons.forEach(button => {
        const selected = Number(button.dataset.viewerIndex) === viewer.currIndex;
        button.classList.toggle("is-active", selected); button.setAttribute("aria-current", String(selected)); button.tabIndex = selected ? 0 : -1;
      });
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (!visible || generation !== currentGroup() || !isCurrentMobileSession(session)) return;
        const selected = strip.querySelector('[aria-current="true"]');
        if (selected) strip.scrollLeft = selected.offsetLeft + selected.offsetWidth / 2 - strip.clientWidth / 2;
        observer?.disconnect();
        if (typeof IntersectionObserver === "function") {
          observer = new IntersectionObserver(entries => { for (const entry of entries) if (entry.isIntersecting) load(entry.target); }, { root: strip, rootMargin: "0px 120px" });
          buttons.forEach(button => observer.observe(button));
        } else buttons.filter(button => Math.abs(Number(button.dataset.viewerIndex) - viewer.currIndex) <= 2).forEach(load);
      });
    }

    function sync() {
      const indices = groups.get(currentGroup()) || [];
      const hasFilmstrip = indices.length > 1;
      const pending = hasFilmstrip && generation !== currentGroup();
      viewer.element.classList.toggle("has-viewer-filmstrip", hasFilmstrip);
      // Keep the glass surface mounted and visible between multi-image groups.
      // Only the old thumbnail content waits for the gesture's idle handoff.
      strip.hidden = !hasFilmstrip;
      strip.toggleAttribute("data-group-pending", pending);
      strip.inert = !visible || pending || !hasFilmstrip;
      strip.setAttribute("aria-busy", String(visible && pending));
      if (generation !== currentGroup() || indices.length <= 1) {
        observer?.disconnect(); queue = []; revision++;
        strip.querySelectorAll("[data-preview-queued]").forEach(button => delete button.dataset.previewQueued);
      }
      // Defer DOM rebuilding and centering until the main image finishes moving.
      if (visible) void queueMobileWork(session, "filmstrip", render);
    }

    strip.addEventListener("click", event => {
      event.stopPropagation();
      const button = event.target.closest("[data-viewer-index]");
      if (button && visible && generation === currentGroup()) viewer.goTo(Number(button.dataset.viewerIndex));
    });
    strip.addEventListener("keydown", event => {
      if (!visible || generation !== currentGroup() || strip.inert) return;
      const button = event.target.closest("[data-viewer-index]");
      if (!button) return;
      const indices = groups.get(generation) || [], offset = indices.indexOf(Number(button.dataset.viewerIndex));
      const target = ({ ArrowLeft: Math.max(0, offset - 1), ArrowRight: Math.min(indices.length - 1, offset + 1), Home: 0, End: indices.length - 1 })[event.key];
      if (target === undefined) return;
      event.preventDefault(); event.stopPropagation();
      viewer.goTo(indices[target]); strip.querySelector(`[data-viewer-index="${indices[target]}"]`)?.focus({ preventScroll: true });
    });
    strip.addEventListener("wheel", event => {
      event.stopPropagation();
      if (event.ctrlKey || event.metaKey || strip.scrollWidth <= strip.clientWidth || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
      event.preventDefault(); strip.scrollLeft += event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? strip.clientWidth : 1);
    }, { passive: false });
    // The strip is a sibling of PhotoSwipe's scrollWrap. Leave native touch
    // scrolling intact and prevent its clicks from becoming main-image taps.
    sync();
    return {
      sync,
      setVisible(value) {
        if (!value && strip.contains(document.activeElement)) viewer.element.focus({ preventScroll: true });
        visible = value; strip.inert = !value; strip.setAttribute("aria-hidden", String(!value));
        if (value) sync();
        else { observer?.disconnect(); queue = []; revision++; strip.querySelectorAll("[data-preview-queued]").forEach(button => delete button.dataset.previewQueued); }
      },
      destroy() { visible = false; revision++; cancelAnimationFrame(frame); observer?.disconnect(); queue = []; strip.remove(); },
    };
  }

  async function prepareMobileViewerBackdrop(item) {
    const displayed = els.drawerBody.querySelector(".detail-image-backdrop:not(.detail-backdrop-previous)");
    // Keep a loaded thumbnail fallback while the candidate is decoded off screen.
    // Opening must never wait indefinitely for a broken preview or image decoder.
    const fallback = displayed?.complete && displayed.naturalWidth > 1 ? displayed.cloneNode(false) : null;
    const withinDeadline = async (work) => {
      let timer;
      try { return await Promise.race([work, new Promise(resolve => { timer = window.setTimeout(() => resolve(null), 800); })]); }
      finally { window.clearTimeout(timer); }
    };
    let background = document.createElement("img");
    background.className = "image-studio-viewer-backdrop";
    try {
      const source = item.previewSrc || await withinDeadline(loadImageMedia(item, "preview"));
      if (source) {
        item.previewSrc = source;
        background.src = source;
        const decoded = await withinDeadline(background.decode().then(() => true));
        if (!decoded || !background.naturalWidth) background = null;
      } else background = null;
    } catch { background = null; }
    if (!background && fallback?.complete && fallback.naturalWidth > 1) background = fallback;
    if (!background) return null;
    background.className = "image-studio-viewer-backdrop";
    background.alt = ""; background.setAttribute("aria-hidden", "true");
    return background;
  }

  async function openMobileImageViewer(dataUrl, context) {
    if (mobileImageViewer || mobileViewerOpening) return true;
    mobileViewerOpening = true;
    const openingRevision = ++mobileViewerOpenRevision;
    const requestedDetailRevision = detailRequestRevision;
    const requestedImageId = state.detailData?.images?.[context.imageIndex]?.id;
    const currentOpening = () => openingRevision === mobileViewerOpenRevision
      && requestedDetailRevision === detailRequestRevision && String(state.detailId) === String(context.generationId)
      && (!requestedImageId || String(state.detailData?.images?.[state.detailImageIndex]?.id) === String(requestedImageId));
    try {
      const sequence = await ensureDetailSequence(detailNavigationSession);
      if (!currentOpening()) return true;
      const initialIndex = sequence.findIndex((item) => String(item.generation_id) === String(context.generationId) && (requestedImageId ? String(item.image_id) === String(requestedImageId) : Number(item.image_index) === Number(context.imageIndex)));
      if (initialIndex < 0 || !sequence.length) return false;
      mobileDetailSyncRevision++;
      mobileImageSequence = sequence;
      mobileImageLoads = new Map();
      const knownImages = new Map(detailDisplayImages(state.detailData || {}, state.detailFallbackThumbnail).map((item, index) => [item.id ? String(item.id) : `index:${index}`, item]));
      const galleryById = new Map(state.galleryItems.map((item) => [String(item.id), item]));
      mobileImageDataSource = sequence.map((item) => {
        const known = mobileSourceForSequenceItem(item, dataUrl, context, knownImages, galleryById);
        const preview = known.detail === "preview" ? known.src : getImageMedia(item, "preview");
        const edge = displayImageEdge(item);
        const display = getImageMedia(item, `display:${edge}`) || (known.detail === "display" ? known.src : "");
        return { ...item, src: display || preview || EMPTY_MOBILE_IMAGE, msrc: preview || display || EMPTY_MOBILE_IMAGE, width: Math.max(1, Number(item.width || 1)), height: Math.max(1, Number(item.height || 1)), previewSrc: preview, displaySrc: display, displayEdge: display ? edge : 0, originalSrc: "", loadedDetail: display ? "display" : preview ? "preview" : "", alt: "生成结果" };
      });
      const initialItem = mobileImageDataSource[initialIndex];
      const initialBackdropImage = await prepareMobileViewerBackdrop(initialItem);
      if (!currentOpening()) return true;
      const initialBackdrop = window.ImageStudioViewerBackdrop.create(initialBackdropImage);
      if (!initialBackdrop) { showNotice("图片预览暂时无法加载，请稍后重试。", "error"); return true; }
      if (initialItem.previewSrc) {
        initialItem.src = initialItem.displaySrc || initialItem.previewSrc; initialItem.msrc = initialItem.previewSrc;
        initialItem.loadedDetail = initialItem.displaySrc ? "display" : "preview";
      }
      const session = { active: true, sequence, items: mobileImageDataSource, loads: mobileImageLoads, retryTimers: new Set(), viewer: null, backdrop: null, status: null, workQueue: new Map(), workTimer: 0, pointerIds: new Set(), touchCount: 0, inputSuspended: false, resettingGesture: false, originalIndices: new Set(mobileImageDataSource.flatMap((item, index) => item.originalSrc ? [index] : [])), preparingDetail: false, detailSyncIndex: -1, detailSyncPromise: null, entryAnimation: null };
      session.sequenceRevision = detailNavigationSession?.sequenceRevision || galleryDataRevision;
      session.displayIndices = new Set(mobileImageDataSource.flatMap((item, index) => item.displaySrc ? [index] : []));
      session.mediaObjects = window.ImageStudioMediaObjects.createScope();
      // PhotoSwipe blocks input during its opening animation; the visual fade is independent.
      const pswp = new window.PhotoSwipe({ dataSource: mobileImageDataSource, index: initialIndex, loop: false, closeOnVerticalDrag: true, pinchToClose: false, tapAction: toggleMobileImageControls, imageClickAction: toggleMobileImageControls, bgClickAction: toggleMobileImageControls, doubleTapAction: "zoom", initialZoomLevel: "fit", secondaryZoomLevel: levels => levels.initial * 2.5, maxZoomLevel: levels => Math.max(levels.initial * 2.5, Math.min(1, levels.initial * 8)), preload: [1, 1], arrowPrev: false, arrowNext: false, close: false, zoom: false, counter: false, bgOpacity: 1, showHideAnimationType: "fade", showAnimationDuration: 0, hideAnimationDuration: 220, zoomAnimationDuration: 220, errorMsg: "图片暂时无法加载，请重试。", mainClass: "image-studio-pswp" });
      session.viewer = pswp;
      pswp.on("contentLoadImage", ({ content }) => { if (content.element?.tagName === "IMG") content.element.decoding = "async"; });
      pswp.on("zoomPanUpdate", () => {
        const wantsOriginal = mobileWantsOriginal(session);
        if (session.wantsOriginal !== wantsOriginal) { session.wantsOriginal = wantsOriginal; syncMobileResolution(session); }
      });
      pswp.on("resolutionChanged", () => syncMobileResolution(session));
      pswp.addFilter("preventPointerEvent", (prevent, event) => event.target instanceof Element && event.target.closest(".image-studio-viewer-filmstrip") ? false : prevent);
      const placeholders = new Map(), destroyedSlides = new WeakSet();
      function syncPlaceholder(slide) {
        if (!session.active || pswp.isDestroying || !slide?.holderElement || destroyedSlides.has(slide)) return;
        let entry = placeholders.get(slide);
        if (!entry) {
          const placeholder = window.ImageStudioImagePlaceholder.create();
          entry = { element: placeholder, displayedImages: new WeakSet(), pending: false };
          const syncAfterPaintChange = () => {
            if (entry.pending) return;
            entry.pending = true;
            queueMicrotask(() => { entry.pending = false; syncPlaceholder(slide); });
          };
          entry.observer = new MutationObserver(syncAfterPaintChange);
          entry.observer.observe(slide.container, { childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset"] });
          // PhotoSwipe's own thumbnail IMG loads independently of its content
          // events; either it or the main IMG can become visible first.
          entry.onImageEvent = syncAfterPaintChange;
          slide.container.addEventListener("load", entry.onImageEvent, true);
          slide.container.addEventListener("error", entry.onImageEvent, true);
          placeholders.set(slide, entry);
          // The holder follows paging but does not zoom or pan the decoration.
          // PhotoSwipe may replace its content/placeholder independently.
          slide.holderElement.appendChild(placeholder);
        }
        const content = slide.content;
        const images = Array.from(slide.container.querySelectorAll("img")).filter(image =>
          !image.hidden && !image.classList.contains("pswp__hidden")
          && !!image.getAttribute("src") && image.getAttribute("src") !== EMPTY_MOBILE_IMAGE);
        const readyImages = images.filter(image => image.complete && image.naturalWidth > 0);
        readyImages.forEach(image => entry.displayedImages.add(image));
        // Keep a displayed preview unobstructed while that same mounted image
        // upgrades to the original. A cold/replaced/error node still needs its marker.
        const upgrading = content.state === "loading" && images.includes(content.element)
          && entry.displayedImages.has(content.element);
        entry.element.classList.toggle("is-ready", readyImages.length > 0 || upgrading);
      }
      pswp.on("firstZoomPan", ({ slide }) => syncPlaceholder(slide));
      for (const event of ["contentLoadImage", "loadComplete", "contentAppendImage", "contentRemove", "contentActivate"]) {
        pswp.on(event, ({ content }) => {
          const slide = content.slide;
          // PhotoSwipe emits these before its DOM update; inspect attachment
          // afterwards, without waiting for HTTP, decoding or a gesture to end.
          if (slide) queueMicrotask(() => syncPlaceholder(slide));
        });
      }
      function removePlaceholder(slide) {
        destroyedSlides.add(slide);
        const entry = placeholders.get(slide);
        if (!entry) return;
        entry.observer.disconnect();
        slide.container.removeEventListener("load", entry.onImageEvent, true);
        slide.container.removeEventListener("error", entry.onImageEvent, true);
        entry.element.remove(); placeholders.delete(slide);
      }
      pswp.on("slideDestroy", ({ slide }) => removePlaceholder(slide));
      pswp.on("destroy", () => { for (const slide of placeholders.keys()) removePlaceholder(slide); });
      pswp.addFilter("placeholderSrc", (_source, content) => content.data.previewSrc || content.data.originalSrc || false);
      pswp.addFilter("contentErrorElement", (element) => {
        // The app owns pending/retry/failure feedback; PhotoSwipe only knows
        // whether its current (possibly temporary) src failed to load.
        element.textContent = ""; element.setAttribute("aria-hidden", "true"); return element;
      });
      pswp.on("loadError", ({ content }) => {
        const item = session.items[content.index];
        if (!item || !isCurrentMobileSession(session, content.index, item)) return;
        if (content.data.src !== EMPTY_MOBILE_IMAGE) item.displayError = "图片暂时无法加载。";
        updateMobileViewerFeedback(session);
      });
      pswp.on("loadComplete", ({ content, isError }) => {
        const item = session.items[content.index];
        if (!item || !isCurrentMobileSession(session, content.index, item)) return;
        // PhotoSwipe can keep isAttached=true when reloading an error DIV on
        // activation. Its append() then skips the replacement IMG entirely.
        // Leave an in-progress native decode to its normal appendImage callback.
        if (!isError && content.isAttached && !content.isDecoding && content.element instanceof HTMLImageElement
          && content.slide?.heavyAppended && !content.element.parentNode) content.appendImage();
        if (!isError && content.data.src !== EMPTY_MOBILE_IMAGE) item.displayError = "";
        updateMobileViewerFeedback(session);
      });
      pswp.on("uiRegister", () => {
        pswp.ui.registerElement({ name: "image-studio-download", className: "pswp__button--image-studio-download", isButton: true, appendTo: "root", ariaLabel: "下载当前图片", html: '<svg class="image-studio-download-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>', onInit: (element) => { element.dataset.tooltip = "下载图片"; }, onClick: () => void downloadMobileImage() });
      });
      pswp.on("change", () => {
        if (!isCurrentMobileSession(session)) return;
        session.filmstrip?.sync();
        // Do not carry the previous slide's error through the next gesture
        // while the idle work queue catches up with its new selection.
        if (session.status) session.status.hidden = true;
        restoreMobileViewerBackground(session);
        const download = pswp.element?.querySelector(".pswp__button--image-studio-download");
        if (download) download.hidden = session.items[pswp.currIndex]?.allowed_actions?.download === false;
        if (session.preparingDetail) { mobileDetailSyncRevision++; detailRequestRevision++; detailImagePaintRevision++; }
        session.preparingDetail = false; session.detailSyncIndex = -1; session.detailSyncPromise = null;
        warmMobileImages(pswp.currIndex, session);
      });
      pswp.on("moveMainScroll", (event) => {
        if (event.dragging && pswp.gestures.dragAxis === "x") restoreMobileViewerBackground(session);
      });
      pswp.on("resize", () => { if (isCurrentMobileSession(session)) { window.ImageStudioViewerBackdrop.resize(session.backdrop); session.filmstrip?.sync(); syncMobileResolution(session); } });
      pswp.on("pointerDown", (event) => trackMobilePointer(session, event, true));
      pswp.on("pointerUp", (event) => trackMobilePointer(session, event, false));
      pswp.on("verticalDrag", () => { void prepareMobileDetail(session); });
      pswp.on("close", () => {
        cancelMobileWork(session);
        window.ImageStudioViewerBackdrop.pause(session.backdrop);
        void prepareMobileDetail(session);
      });
      pswp.on("afterInit", () => {
        bindMobileGestureInterruption(session);
        session.filmstrip = createMobileFilmstrip(session);
        const download = pswp.element?.querySelector(".pswp__button--image-studio-download");
        if (download) download.hidden = session.items[pswp.currIndex]?.allowed_actions?.download === false;
        const backgroundLayer = document.createElement("div"); backgroundLayer.className = "image-studio-viewer-background"; backgroundLayer.setAttribute("aria-hidden", "true");
        backgroundLayer.appendChild(initialBackdrop); pswp.bg?.appendChild(backgroundLayer); session.backdrop = initialBackdrop;
        // The opaque canvas is already painted before the first visible frame.
        session.themeObserver = new MutationObserver(() => updateMobileViewerFeedback(session));
        session.themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
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
      pswp.on("openingAnimationEnd", () => { pswp.element?.classList.remove("image-studio-controls-visible"); session.filmstrip?.setVisible(false); });
      pswp.on("destroy", () => {
        const item = session.sequence[pswp.currIndex];
        const shouldSyncDetail = !suppressMobileDetailSync;
        session.active = false;
        session.items.forEach(item => forgetDecodedImage(item.originalSrc));
        session.mediaObjects.dispose();
        session.filmstrip?.destroy();
        cancelMobileWork(session);
        session.themeObserver?.disconnect();
        window.ImageStudioViewerBackdrop.dispose(session.backdrop);
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
    els.downloadImageButton.hidden = item.allowed_actions?.download === false;
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
      if (item.allowed_actions?.download === false) return;
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
    window.ImageStudioDialogMotion.show(els.imagePreview);
    syncPageScrollLock();
  }

  function closeImagePreview() {
    if (els.imagePreview.classList.contains("is-hidden") || els.imagePreview.classList.contains("is-closing")) return;
    syncDetailFromImagePreview();
    state.imagePreviewItems = []; state.imagePreviewIndex = 0; state.imagePreviewDownloadFilename = ""; state.imagePreviewContext = null; state.imagePreviewNavigating = false; state.imagePreviewSwipeAt = 0;
    window.ImageStudioDialogMotion.hide(els.imagePreview, () => {
      els.previewImage.removeAttribute("src"); els.downloadImageButton.href = "#"; els.downloadImageButton.download = "";
      syncPageScrollLock();
    });
    syncPageScrollLock();
  }

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
      const currentImage = state.detailData?.images?.[state.detailImageIndex];
      if ([currentImage?.supplemental?.generation_engine, state.detailData?.generation_engine, currentImage?.metadata?.format, state.detailData?.provider_kind].some(value => String(value || "").toLowerCase() === "comfyui")) {
        if (await comfyuiControls.fromGallery(state.detailData, currentImage)) return;
      }
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
    if (draft.temporary_model) {
      const provider = state.providers.find(item => item.id === draft.provider_id && item.kind === "comfyui" && item.enabled !== false);
      if (!provider) throw new Error("目标 ComfyUI 服务商已停用或删除，请重新选择。");
      const model = structuredClone(draft.temporary_model);
      state.comfyuiTemporaryModel = { ...model, model_ref: draft.model_ref || `${provider.id}:${model.id}`, provider_id: provider.id, provider_name: provider.name, provider_kind: "comfyui", temporary: true, seed_warnings: draft.seed_warnings || [] };
    }
    state.comfyuiSnapshot = draft.comfyui || null;
    state.mode = draft.mode === "img2img" ? "img2img" : "text2img"; state.selectedProviderId = draft.provider_id || ""; state.references = draft.references || [];
    state.selectedModelRef = draft.model_ref || (draft.provider_id && draft.model ? `${draft.provider_id}:${draft.model}` : "");
    state.comfyuiModelOverride = draft.comfyui_model ? { model_ref: state.selectedModelRef, model: draft.comfyui_model } : null;
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

  function modelParameterMatches(name, descriptor, keys) { return keys.includes(name) || keys.includes(descriptor?.request_key); }
  function parameterAppliesToMode(descriptor) { return !Array.isArray(descriptor?.modes) || descriptor.modes.includes(state.mode); }
  function effectiveModelParameters(model) { return Object.entries(model?.parameters || {}).filter(([name, descriptor]) => !modelParameterMatches(name, descriptor, MODEL_SCHEDULING_FIELDS)); }
  const MODEL_SCHEDULING_FIELDS = ["batch_mode", "concurrency", "native_count_supported", "native_batch_size", "native_batch_size_source", "max_concurrent_requests"];

  function dataUrlToFile(dataUrl, name) { const [head, encoded] = dataUrl.split(",", 2); const type = (head.match(/data:([^;]+)/) || [])[1] || "image/png"; const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0)); return new File([bytes], name, { type }); }
  async function checkGalleryAction(ids, action) {
    const preview = await apiPost("gallery/delete/preview", { ids, action });
    if (preview.allowed === false || preview.denied?.length) {
      const messages = (preview.denied || []).map(item => item.message || `${item.source_name || item.source_id || "外部图库"}未允许此操作`);
      throw new Error(Array.from(new Set(messages)).join("；") || "所选图片包含未允许此操作的外部资源。");
    }
    return preview;
  }
  async function exportSelected() { try { const ids = Array.from(state.selectedIds); await checkGalleryAction(ids, "download"); const result = await apiPost("gallery/export", { ids }); const client = await bridge(); await client.download(result.download_endpoint, {}, result.filename); showNotice("导出文件已开始下载。", "success"); } catch (error) { showNotice(errorMessage(error, "画廊导出失败"), "error"); } }
  async function deleteSelected() {
    const ids = Array.from(state.selectedIds); if (!ids.length || $("deleteButton").disabled) return;
    $("deleteButton").disabled = true;
    try {
      // Selections survive paging, so visible cards cannot establish whether
      // the whole operation includes external originals.
      const preview = await checkGalleryAction(ids, "delete");
      const external = Number(preview.external_count || 0) > 0;
      const names = (preview.external_sources || []).map(source => typeof source === "string" ? source : source.name || source.id).filter(Boolean).join("、");
      const warning = external ? `其中包含 ${preview.external_count} 条外部记录${names ? `（${names}）` : ""}，会永久删除来源目录中的原图及已确认关联的参数文件，无法恢复。` : "";
      if (!await confirmAction(`永久删除 ${ids.length} 条生成记录及其结果图？${warning}`)) return;
      const result = await apiPost("gallery/delete", { ids, confirm_external: external });
      const errors = result.errors || [];
      const confirmedDeleted = new Set(Array.isArray(result.deleted) ? result.deleted : []);
      const failed = new Set([...(result.failed || []), ...errors.map(error => error.id)].filter(id => id && !confirmedDeleted.has(id)));
      const deleted = Array.isArray(result.deleted) ? result.deleted : ids.filter(id => !failed.has(id));
      for (const id of deleted) state.selectedIds.delete(id);
      for (const id of failed) state.selectedIds.add(id);
      updateSelection(); await loadGallery();
      showNotice(errors.length ? `已删除 ${deleted.length} 条记录；${errors.map(error => error.message || "删除失败").join("；")}${failed.size ? "。未删除的记录仍保持勾选。" : ""}` : "所选生成记录已删除。", errors.length ? "error" : "success");
    } catch (error) { showNotice(errorMessage(error, "生成记录删除失败"), "error"); }
    finally { $("deleteButton").disabled = false; }
  }

  function applyUploadedReference(uploaded) {
    state.references = [uploaded]; state.mode = "img2img";
    document.querySelectorAll(".segment").forEach(button => button.classList.toggle("is-active", button.dataset.mode === "img2img"));
    renderModelChoices(); renderReferences(); closeDetail(); switchView("generate");
    setError(els.generationError, "已将当前成图作为新的图生图参考图。它不会被当作历史原始参考图。");
  }

  async function useDataUrlAsReference(dataUrl, name) {
    try { const client = await bridge(); applyUploadedReference(await client.upload("studio/reference/upload", dataUrlToFile(dataUrl, name))); }
    catch (error) { showNotice(errorMessage(error, "添加参考图失败"), "error"); }
  }

  async function useGalleryImageAsReference(image) {
    if (!image?.id || image.allowed_actions?.reference === false || state.detailData?.allowed_actions?.reference === false) return;
    $("detailUseReference").disabled = true;
    try { applyUploadedReference(await apiPost("studio/reference/from-gallery", { image_id: image.id })); }
    catch (error) { showNotice(errorMessage(error, "添加参考图失败"), "error"); }
    finally { library.updateDetailActions(state.detailData); }
  }
  function confirmAction(message) {
    activeConfirmation?.(false, true);
    const previousFocus = document.activeElement;
    return new Promise(resolve => {
      const dialog = $("confirmDialog"), cancel = $("confirmCancel"), accept = $("confirmAccept");
      let closing = false;
      const finish = (value, immediate = false) => {
        if (activeConfirmation !== finish || (closing && !immediate)) return;
        closing = true;
        cancel.removeEventListener("click", onCancel); accept.removeEventListener("click", onAccept); document.removeEventListener("keydown", onKeydown);
        const complete = () => {
          if (activeConfirmation !== finish) return;
          activeConfirmation = null;
          if (!immediate) { syncPageScrollLock(); if (previousFocus?.isConnected) previousFocus.focus?.({ preventScroll: true }); }
          resolve(value);
        };
        if (immediate) { window.ImageStudioDialogMotion.hideImmediately(dialog); complete(); }
        else { window.ImageStudioDialogMotion.hide(dialog, complete); syncPageScrollLock(); }
      };
      const onCancel = () => finish(false), onAccept = () => finish(true);
      const onKeydown = event => { if (event.key === "Escape" && !library.modalOpen()) finish(false); };
      $("confirmMessage").textContent = message;
      activeConfirmation = finish;
      window.ImageStudioDialogMotion.show(dialog); syncPageScrollLock(); accept.focus({ preventScroll: true });
      cancel.addEventListener("click", onCancel); accept.addEventListener("click", onAccept); document.addEventListener("keydown", onKeydown);
    });
  }

  function bindEvents() {
    if (eventsBound) return;
    eventsBound = true;
    for (const [id] of galleryFilterFields) {
      const select = $(id);
      const selection = window.ImageStudioSelect?.getGalleryDefault(id) || { mode: "all" };
      galleryFilterSelections.set(id, selection);
      for (const option of select.options) option.selected = selection.mode === "all" || selection.values.includes(option.value);
      // Register before other change handlers can issue a gallery request.
      select.addEventListener("change", () => {
        const values = Array.from(select.selectedOptions, option => option.value);
        galleryFilterSelections.set(id, select.options.length && values.length === select.options.length ? { mode: "all" } : { mode: "values", values });
      });
    }
    window.addEventListener("image-studio-gallery-default-saved", event => {
      const { id, selection } = event.detail || {};
      if (galleryFilterSelections.has(id) && selection && event.detail.selectionUnchanged !== false) {
        galleryFilterSelections.set(id, selection);
        if (state.view === "gallery") void loadGallery(0);
      }
      showNotice("已设为此浏览器的默认筛选。", "success");
    });
    window.addEventListener("image-studio-gallery-default-error", event => showNotice(event.detail?.message || "默认筛选保存失败。", "error"));
    library.bind();
    settings.bind();
    const refreshQuota = () => refreshProviderQuota();
    providerQuotaTimer = window.setInterval(refreshQuota, PROVIDER_QUOTA_TTL);
    document.addEventListener("visibilitychange", refreshQuota);
    window.addEventListener("focus", refreshQuota);
    window.addEventListener("pagehide", () => { window.clearInterval(providerQuotaTimer); providerQuotaTimer = 0; });
    window.addEventListener("pageshow", () => { if (!providerQuotaTimer) providerQuotaTimer = window.setInterval(refreshQuota, PROVIDER_QUOTA_TTL); refreshQuota(); });
    window.addEventListener("resize", () => {
      if (!state.detailId) return;
      centerDetailFilmstrip(els.drawerBody.querySelector(".detail-filmstrip"));
      if (touchImageDisplay() && state.detailData && !mobileViewerSession?.active) void loadDetailAssets(state.detailId, state.detailData, state.detailFallbackThumbnail);
    }, { passive: true });
    els.galleryGrid.addEventListener("click", (event) => { const card = event.target.closest("[data-gallery-id]"); if (card && !event.target.closest(".gallery-selection")) void openDetail(card.dataset.galleryId); });
    els.galleryGrid.addEventListener("change", (event) => { const input = event.target.closest("[data-select-id]"); if (!input) return; input.checked ? state.selectedIds.add(input.dataset.selectId) : state.selectedIds.delete(input.dataset.selectId); updateSelection(); });
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
    document.querySelectorAll(".segment").forEach((button) => button.addEventListener("click", () => applyGenerationSelection(button.dataset.mode, state.defaultModelRefs[button.dataset.mode] || "")));
    els.modelChoice.addEventListener("change", () => {
      const value = els.modelChoice.value;
      if (value.startsWith("@comfy:")) applyGenerationSelection(state.mode, "", value.slice(7));
      else applyGenerationSelection(state.mode, value);
    });
    els.comfyWorkflowChoice.addEventListener("change", () => applyGenerationSelection(state.mode, els.comfyWorkflowChoice.value, state.selectedProviderId));
    els.resetNegativePromptButton.addEventListener("click", () => { els.negativePrompt.value = selectedModel()?.negative_prompt_default || ""; els.negativePrompt.focus(); });
    $("referenceChooseButton").addEventListener("click", () => els.referenceUpload.click());
    els.referenceUpload.addEventListener("change", async () => { setError(els.generationError, ""); try { await uploadReferences(els.referenceUpload.files); } catch (error) { setError(els.generationError, errorMessage(error, "上传参考图失败")); } finally { els.referenceUpload.value = ""; } });
    els.generationForm.addEventListener("submit", generate); $("galleryRefresh").addEventListener("click", () => void loadGallery()); $("galleryRetry").addEventListener("click", () => void loadGallery()); els.galleryPrev.addEventListener("click", () => void loadGallery(state.galleryPage - 1)); els.galleryNext.addEventListener("click", () => void loadGallery(state.galleryPage + 1)); els.gallerySearch.addEventListener("change", () => void loadGallery(0)); els.galleryProvider.addEventListener("change", () => void loadGallery(0)); els.galleryMode.addEventListener("change", () => void loadGallery(0)); els.gallerySource.addEventListener("change", () => void loadGallery(0));
    $("cancelSelectionButton").addEventListener("click", clearGallerySelection); $("selectAllButton").addEventListener("click", () => { state.galleryItems.forEach((item) => state.selectedIds.add(item.id)); els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => { input.checked = true; }); updateSelection(); }); $("exportButton").addEventListener("click", () => void exportSelected()); $("deleteButton").addEventListener("click", () => void deleteSelected());
    $("closeDrawer").addEventListener("click", closeDetail); $("closeImagePreview").addEventListener("click", closeImagePreview); els.imagePreviewPrev.addEventListener("click", () => void navigateImagePreview(-1)); els.imagePreviewNext.addEventListener("click", () => void navigateImagePreview(1)); bindImagePreviewGestures(); els.imagePreview.querySelector("[data-close-image-preview]").addEventListener("click", closeImagePreview); els.previewImage.addEventListener("click", () => { if (Date.now() - state.imagePreviewSwipeAt < 500) return; closeImagePreview(); }); els.scrim.addEventListener("click", () => { if (!els.parameterDialog.classList.contains("is-hidden")) return; if (activeConfirmation) activeConfirmation(false); else closeDetail(); });
    document.addEventListener("keydown", (event) => { if (mobileImageViewer || library.modalOpen()) return; if (event.key === "Escape") { if (!els.parameterDialog.classList.contains("is-hidden")) closeToolParameterDialog(); else if (!els.imagePreview.classList.contains("is-hidden")) closeImagePreview(); else if (els.detailDrawer.classList.contains("is-open") && !activeConfirmation) closeDetail(); return; } if (event.target.closest('input,textarea,select,[role="combobox"],[contenteditable=true]')) return; if (!els.imagePreview.classList.contains("is-hidden")) { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); void navigateImagePreview(event.key === "ArrowLeft" ? -1 : 1); } return; } if (!els.detailDrawer.classList.contains("is-open") || activeConfirmation) return; if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); void navigateDetail(event.key === "ArrowLeft" ? -1 : 1); } });
  }

  const modal = window.ImageStudioModal({ escape, showNotice, errorMessage, syncPageScrollLock });
  const library = window.ImageStudioLibrary({ state, modal, escape, apiGet, apiPost, bridge, showNotice, errorMessage, formatDate, formatBytes, sourceLabel, getGallerySort: () => gallerySort, syncPageScrollLock, switchView, requestParameters, loadGallery, clearGallerySelection, openDetail, closeDetail, reproduce, applyDraft, useDataUrlAsReference, useGalleryImageAsReference, ensureDetailMetadata, ensureDetailPreview, getImageMedia, cacheImageMedia, loadImageMedia, checkGalleryAction });
  const settings = window.ImageStudioSettings({
    escape, apiGet, apiPost, showNotice, errorMessage, formatDate, formatBytes, setError, confirmAction,
    syncPageScrollLock, schemaParameterTitle, schemaParameterLabel, configuredReferenceLimit, referenceLimitForModel,
    effectiveModelParameters, modelParameterMatches, bootstrap, closeDetail, switchView, library,
    getView: () => state.view, showView, confirmationOpen: () => !!activeConfirmation,
    getGallerySort: () => gallerySort,
    onGallerySortSaved: value => { gallerySort = value; invalidateBrowseCache(); state.galleryPage = 0; },
    externalSources: () => externalSources, comfyui: () => comfyuiControls,
  });
  const { loadSettings, loadStorageHealth, updateSettingsDirty, currentSettingsModel, renderModelEditor, prepareComfyWorkflowSettings, addComfyWorkflowDraft, closeToolParameterDialog } = settings;
  const externalSources = window.ImageStudioExternalSources({ getView: () => state.view, getSettings: settings.getSettings, apiGet, apiPost, escape, formatBytes, formatDate, showNotice, errorMessage, updateSettingsDirty, invalidateBrowseCache, openModal: (...args) => library.openModal(...args) });
  const novelaiControls = window.ImageStudioNovelAI({ state, model: selectedModel, escape, schemaParameterTitle, schemaParameterLabel, rerenderReferences: renderReferences, uploadFile: async file => (await bridge()).upload("studio/reference/upload", file), openModal: (...args) => library.openModal(...args), showNotice });
  const comfyuiControls = window.ImageStudioComfyUI({ state, escape, apiGet, apiPost, bridge, showNotice, errorMessage, currentSettingsModel, renderModelEditor, updateSettingsDirty, applyDraft, prepareWorkflowSettings: prepareComfyWorkflowSettings, addWorkflowDraft: addComfyWorkflowDraft, invalidateBrowseCache, renderResult: renderGenerationResult, viewResult: viewComfyResult, openModal: (...args) => library.openModal(...args) });

  async function start() {
    try {
      await window.ImageStudioGalleryPreferences.ready(await bridge());
      gallerySort = window.ImageStudioGalleryPreferences.getSort();
      settings.resetGallerySort();
    } catch { showNotice("未能读取此浏览器的画廊偏好，暂用默认显示。", "error"); }
    bindEvents();
    try { await bootstrap(); void comfyuiControls.restore(); }
    catch (error) { const message = errorMessage(error, "页面初始化失败"); els.runtimeStatus.textContent = "页面初始化失败"; showNotice(message, "error"); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => void start(), { once: true });
  else void start();
})();
