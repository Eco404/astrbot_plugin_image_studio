(function () {
  "use strict";

  window.__imageStudioAppLoaded = true;

  const state = {
    view: "generate", mode: "text2img", providers: [], selectedProviderId: "", references: [],
    resultImages: [], galleryItems: [], selectedIds: new Set(), settings: null, selectedSettingsProviderId: "", detailId: "",
  };
  let activeConfirmation = null;
  let settingsLoadPromise = null;
  let eventsBound = false;
  const $ = (id) => document.getElementById(id);
  const els = {
    pageTitle: $("pageTitle"), pageSubtitle: $("pageSubtitle"), runtimeStatus: $("runtimeStatus"), providerStatus: $("providerStatus"),
    providerChoices: $("providerChoices"), referenceField: $("referenceField"), referenceUpload: $("referenceUpload"), referenceStrip: $("referenceStrip"),
    generationForm: $("generationForm"), prompt: $("prompt"), negativePromptField: $("negativePromptField"), negativePrompt: $("negativePrompt"), negativePromptHint: $("negativePromptHint"), model: $("model"), size: $("size"), count: $("count"), parameters: $("parameters"), generationError: $("generationError"), generateButton: $("generateButton"), resultEmpty: $("resultEmpty"), resultGrid: $("resultGrid"), resultMeta: $("resultMeta"),
    galleryGrid: $("galleryGrid"), galleryEmpty: $("galleryEmpty"), gallerySearch: $("gallerySearch"), galleryProvider: $("galleryProvider"), galleryMode: $("galleryMode"), selectionBar: $("selectionBar"), selectionCount: $("selectionCount"),
    detailDrawer: $("detailDrawer"), drawerBody: $("drawerBody"), detailDate: $("detailDate"), scrim: $("scrim"), imagePreview: $("imagePreview"), previewImage: $("previewImage"), imagePreviewTitle: $("imagePreviewTitle"), downloadImageButton: $("downloadImageButton"),
    settingEnabled: $("settingEnabled"), settingTool: $("settingTool"), settingConcurrent: $("settingConcurrent"), historyEnabled: $("historyEnabled"), retainReferences: $("retainReferences"), historyRecords: $("historyRecords"), historyMegabytes: $("historyMegabytes"), settingsProviderList: $("settingsProviderList"), providerForm: $("providerForm"), settingsError: $("settingsError"), addProviderButton: $("addProviderButton"), saveSettingsButton: $("saveSettingsButton"),
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

  function providerForMode() { return state.providers.filter((item) => state.mode === "text2img" ? item.supports_text2img : item.supports_img2img); }
  function selectedProvider() { return state.providers.find((item) => item.id === state.selectedProviderId) || null; }
  function text(value) { return String(value || ""); }
  function escape(value) { const div = document.createElement("div"); div.textContent = text(value); return div.innerHTML; }
  function formatDate(value) { return new Date(Number(value) * 1000).toLocaleString(); }
  function formatBytes(value) { const bytes = Number(value || 0); return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(0, Math.round(bytes / 1024))} KB`; }
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
    const labels = { generate: ["生图", "选择模式和生图服务商后开始创作"], gallery: ["画廊", "搜索、筛选、复现或导出历史生成记录"], settings: ["设置", "管理运行策略、历史和生图服务商"], };
    els.pageTitle.textContent = labels[view][0]; els.pageSubtitle.textContent = labels[view][1];
    if (view === "gallery") void loadGallery();
    if (view === "settings") void loadSettings();
  }

  function renderProviderChoices() {
    const available = providerForMode();
    if (!available.some((item) => item.id === state.selectedProviderId)) state.selectedProviderId = available[0]?.id || "";
    els.providerChoices.innerHTML = available.length ? available.map((item) => `<button class="provider-chip ${item.id === state.selectedProviderId ? "is-active" : ""}" type="button" data-provider-id="${escape(item.id)}">${escape(item.name)}<small> ${escape(item.model)}</small></button>`).join("") : '<div class="provider-empty">当前模式没有可用的生图服务商，请先前往设置完成配置。</div>';
    els.providerChoices.querySelectorAll("[data-provider-id]").forEach((button) => button.addEventListener("click", () => { state.selectedProviderId = button.dataset.providerId; renderProviderChoices(); renderGenerationForm(); }));
    renderGenerationForm();
  }

  function renderGenerationForm() {
    const provider = selectedProvider();
    const supportsRefs = state.mode === "img2img";
    const supportsNegative = !!provider?.supports_negative_prompt;
    els.referenceField.classList.toggle("is-hidden", !supportsRefs);
    els.negativePromptField.classList.toggle("is-disabled", !supportsNegative);
    els.negativePrompt.disabled = !supportsNegative;
    els.negativePrompt.placeholder = supportsNegative ? "可选" : "当前服务商不支持专用反向提示词";
    els.negativePromptHint.textContent = supportsNegative ? "当前服务商会将此字段作为专用反向提示词发送。" : "当前服务商没有专用反向提示词参数；可将限制写入正向提示词。";
    els.generateButton.disabled = !provider;
    els.providerStatus.textContent = provider ? `${provider.name} · ${provider.kind}` : "未配置生图服务商";
    if (provider && !els.model.value) els.model.value = provider.model || "";
    renderReferences();
  }

  function renderReferences() {
    els.referenceStrip.innerHTML = state.references.map((item, index) => `<div class="reference-item"><img src="${item.preview_data_url}" alt="参考图 ${index + 1}" /><button type="button" data-reference-index="${index}" aria-label="移除参考图">×</button></div>`).join("");
    els.referenceStrip.querySelectorAll("[data-reference-index]").forEach((button) => button.addEventListener("click", () => { state.references.splice(Number(button.dataset.referenceIndex), 1); renderReferences(); }));
  }

  async function bootstrap() {
    const payload = await apiGet("studio/bootstrap");
    state.providers = Array.isArray(payload.providers) ? payload.providers : [];
    els.size.value = payload.defaults?.size || "1024x1024";
    els.count.value = payload.defaults?.count || 1;
    state.selectedProviderId = payload.defaults?.provider_id || "";
    els.runtimeStatus.textContent = payload.enabled ? `已加载 ${state.providers.length} 个生图服务商` : "生图工作台已关闭";
    renderProviderChoices();
  }

  async function uploadReferences(files) {
    const provider = selectedProvider();
    const maximum = provider?.max_reference_images || 1;
    const available = Math.max(0, maximum - state.references.length);
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
    const provider = selectedProvider();
    if (!provider) { setError(els.generationError, "请先配置支持当前模式的生图服务商"); return; }
    if (state.mode === "img2img" && !state.references.length) { setError(els.generationError, "图生图需要至少一张参考图"); return; }
    els.generateButton.disabled = true; els.generateButton.textContent = "生成中";
    try {
      const result = await apiPost("studio/generate", { mode: state.mode, provider_id: provider.id, prompt: els.prompt.value, negative_prompt: els.negativePrompt.value, model: els.model.value, size: els.size.value, count: Number(els.count.value), parameters, reference_ids: state.references.map((item) => item.id) });
      state.resultImages = result.images || []; state.references = []; renderReferences();
      els.resultEmpty.classList.toggle("is-hidden", state.resultImages.length > 0); els.resultGrid.innerHTML = state.resultImages.map((image, index) => `<div class="result-card"><div class="result-frame"><img src="${image.data_url}" alt="生成结果" data-result-preview="${index}" /></div><div class="result-card-actions"><button class="quiet-button" data-result-reference="${index}" type="button">用作参考图</button></div></div>`).join("");
      els.resultGrid.querySelectorAll("[data-result-reference]").forEach((button) => button.addEventListener("click", () => void useDataUrlAsReference(state.resultImages[Number(button.dataset.resultReference)].data_url, "generated-reference.png")));
      els.resultGrid.querySelectorAll("[data-result-preview]").forEach((image) => image.addEventListener("click", () => openImagePreview(image.src, `生成结果-${Number(image.dataset.resultPreview) + 1}`)));
      els.resultMeta.textContent = `${result.provider_name} · ${result.model} · ${(result.elapsed_ms / 1000).toFixed(1)} 秒${result.generation_id ? " · 已保存到画廊" : " · 历史未保留"}`;
    } catch (error) { setError(els.generationError, errorMessage(error, "生成失败")); }
    finally { els.generateButton.disabled = false; els.generateButton.textContent = "生成图片"; }
  }

  async function loadGallery() {
    try {
      const payload = await apiGet("gallery/list", { query: els.gallerySearch.value, provider_id: els.galleryProvider.value, mode: els.galleryMode.value });
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
    els.drawerBody.innerHTML = `${displayImages.length ? `<div class="detail-images">${displayImages.map((item, index) => item.data_url ? `<div class="detail-image-frame"><img class="detail-image" src="${escape(item.data_url)}" alt="生成结果 ${index + 1}" data-detail-image="${index}" /></div>` : "").join("")}</div>` : '<div class="detail-loading">正在读取生成图片…</div>'}<div class="detail-block"><h3>提示词</h3><pre>${escape(detail.original_prompt)}</pre></div><div class="detail-block"><h3>请求参数</h3><pre>${escape(JSON.stringify(requestParameters(detail), null, 2))}</pre></div><div class="detail-block"><h3>信息</h3><pre>${escape(JSON.stringify({ 服务商: detail.provider_name, 模型: detail.model, 模式: detail.mode === "img2img" ? "图生图" : "文生图", 来源: detail.source, 生成时间: formatDate(detail.created_at), 图片数量: images.length, 文件大小: formatBytes(totalBytes), 耗时毫秒: detail.elapsed_ms }, null, 2))}</pre></div><div class="detail-block"><h3>参考图</h3><div class="detail-references">${refs.length ? refs.map((item) => item.available ? item.data_url ? `<article class="detail-reference"><img src="${escape(item.data_url)}" alt="${escape(item.filename)}" data-detail-reference="${item.id}" /><div>${escape(item.filename)}<br>${formatBytes(item.size_bytes)}</div><button class="danger-button" data-reference-delete="${item.id}" type="button">删除参考图</button></article>` : `<article class="detail-reference"><div>${escape(item.filename)}<br>参考图正在加载…</div></article>` : `<article class="detail-reference"><div>参考图已删除</div></article>`).join("") : "<span>该记录没有保留参考图</span>"}</div></div><div class="detail-block detail-actions"><button class="primary-button" data-reproduce="${detail.id}" type="button">复现参数</button><button class="quiet-button" data-copy-request="${detail.id}" type="button">复制请求参数</button>${images[0]?.data_url ? ' <button class="quiet-button" data-output-reference="1" type="button">将当前成图用作新参考图</button>' : '<span class="field-hint">高清图片仍在加载，请稍候。</span>'}</div>`;
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
      els.prompt.value = draft.prompt || ""; els.negativePrompt.value = draft.negative_prompt || ""; els.model.value = draft.model || ""; els.size.value = draft.size || ""; els.count.value = draft.count || 1; els.parameters.value = JSON.stringify(draft.parameters || {}, null, 2);
      document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === state.mode)); renderProviderChoices(); closeDetail(); switchView("generate");
      if (draft.notice) setError(els.generationError, draft.notice);
    } catch (error) { showNotice(errorMessage(error, "复现参数读取失败"), "error"); }
  }

  async function loadSettings() {
    if (settingsLoadPromise) return settingsLoadPromise;
    els.addProviderButton.disabled = true; els.saveSettingsButton.disabled = true;
    setError(els.settingsError, "正在读取设置…");
    settingsLoadPromise = (async () => {
      try {
        const payload = await apiGet("settings/get");
        if (!payload?.base || !payload?.webui || !Array.isArray(payload.webui.providers)) throw new Error("设置接口返回的数据格式无效");
        state.settings = payload;
        els.settingEnabled.checked = !!payload.base.enabled; els.settingTool.checked = !!payload.base.enable_llm_tool; els.settingConcurrent.value = payload.base.max_concurrent_generations;
        const history = payload.webui.history; els.historyEnabled.checked = !!history.enabled; els.retainReferences.checked = !!history.retain_reference_images; els.historyRecords.value = history.max_records; els.historyMegabytes.value = history.max_megabytes;
        if (!payload.webui.providers.some((item) => item.id === state.selectedSettingsProviderId)) state.selectedSettingsProviderId = payload.webui.providers[0]?.id || "";
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
        els.addProviderButton.disabled = false; els.saveSettingsButton.disabled = false;
      }
    })();
    const loaded = await settingsLoadPromise;
    settingsLoadPromise = null;
    return loaded;
  }

  function currentSettingsProvider() { return state.settings?.webui.providers.find((item) => item.id === state.selectedSettingsProviderId) || null; }
  function renderSettingsProviders() {
    const providers = state.settings?.webui.providers || []; els.settingsProviderList.innerHTML = providers.length ? providers.map((item) => `<button class="provider-row ${item.id === state.selectedSettingsProviderId ? "is-active" : ""}" type="button" data-settings-provider="${escape(item.id)}"><strong>${escape(item.name || item.id)}</strong><span>${item.enabled ? "启用" : "停用"}</span></button>`).join("") : '<div class="provider-empty">尚未添加生图服务商</div>';
    els.settingsProviderList.querySelectorAll("[data-settings-provider]").forEach((button) => button.addEventListener("click", () => { state.selectedSettingsProviderId = button.dataset.settingsProvider; renderSettingsProviders(); }));
    renderProviderEditor();
  }
  function renderProviderEditor() {
    const provider = currentSettingsProvider(); if (!provider) { els.providerForm.innerHTML = '<div class="provider-empty">选择或新增生图服务商后编辑详细配置。</div>'; return; }
    const kinds = [["openai_images", "OpenAI Images"], ["gemini", "Gemini"], ["nai_direct", "NAI 直连"], ["custom_json", "自定义 JSON"]];
    els.providerForm.innerHTML = `<h3>${escape(provider.name || "生图服务商")}</h3>${field("id", "ID", provider.id)}${field("name", "名称", provider.name)}${selectField("kind", "类型", provider.kind, kinds)}${field("base_url", "接口地址（Base URL）", provider.base_url)}${field("model", "默认模型", provider.model)}${field("generate_path", "文生图路径", provider.generate_path)}${field("edit_path", "图生图路径", provider.edit_path)}${field("api_key", "接口密钥（API Key）", provider.api_key)}${field("timeout_seconds", "超时秒数", provider.timeout_seconds, "number")}${selectField("edit_request_format", "图生图格式", provider.edit_request_format, [["multipart", "multipart"], ["json_data_url", "JSON data URL"]])}${toggleField("enabled", "启用", provider.enabled)}${toggleField("supports_text2img", "支持文生图", provider.supports_text2img)}${toggleField("supports_img2img", "支持图生图", provider.supports_img2img)}${toggleField("supports_negative_prompt", "支持专用反向提示词", provider.supports_negative_prompt)}${field("max_reference_images", "最大参考图数", provider.max_reference_images, "number")}${textAreaField("custom_headers", "自定义请求头", provider.custom_headers)}${textAreaField("request_template", "自定义 JSON 请求模板", provider.request_template)}${field("response_image_path", "响应图片路径", provider.response_image_path)}<div class="provider-editor-actions"><button class="danger-button" id="removeProviderButton" type="button">删除服务商</button><button class="quiet-button" id="testProviderButton" type="button">测试服务商</button></div>`;
    els.providerForm.querySelectorAll("[data-provider-field]").forEach((input) => input.addEventListener("input", () => updateProviderField(input))); els.providerForm.querySelectorAll("[data-provider-field]").forEach((input) => input.addEventListener("change", () => updateProviderField(input)));
    $("removeProviderButton")?.addEventListener("click", async () => { if (!await confirmAction("删除此生图服务商？历史记录不会删除。")) return; state.settings.webui.providers = state.settings.webui.providers.filter((item) => item.id !== provider.id); state.selectedSettingsProviderId = state.settings.webui.providers[0]?.id || ""; renderSettingsProviders(); showNotice("已从设置草稿中删除，保存全部设置后生效。", "success"); });
    $("testProviderButton")?.addEventListener("click", () => void testProvider(provider));
  }
  function field(key, label, value, type = "text") { return `<div class="field"><label>${label}</label><input data-provider-field="${key}" type="${type}" value="${escape(value)}" /></div>`; }
  function textAreaField(key, label, value) { return `<div class="field field-wide"><label>${label}</label><textarea data-provider-field="${key}" rows="3">${escape(value)}</textarea></div>`; }
  function selectField(key, label, value, options) { return `<div class="field"><label>${label}</label><select data-provider-field="${key}">${options.map(([id, name]) => `<option value="${id}" ${id === value ? "selected" : ""}>${name}</option>`).join("")}</select></div>`; }
  function toggleField(key, label, value) { return `<div class="toggle-row"><label>${label}</label><label class="toggle-control"><input data-provider-field="${key}" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`; }
  function updateProviderField(input) { const provider = currentSettingsProvider(); if (!provider) return; const key = input.dataset.providerField; provider[key] = input.type === "checkbox" ? input.checked : input.value; if (key === "id") state.selectedSettingsProviderId = input.value; if (key === "kind" && input.value === "nai_direct" && !provider.supports_negative_prompt) { provider.supports_negative_prompt = true; renderProviderEditor(); } }
  async function testProvider(provider) {
    const button = $("testProviderButton");
    if (button) { button.disabled = true; button.textContent = "测试中…"; }
    setError(els.settingsError, "正在测试生图服务商连接…");
    try {
      const payload = await apiPost("provider/test", { provider });
      setError(els.settingsError, ""); showNotice(`服务商测试成功，返回 ${payload.image_count} 张图片。`, "success");
    } catch (error) {
      const message = errorMessage(error, "服务商测试失败"); setError(els.settingsError, message); showNotice(message, "error");
    } finally { if (button) { button.disabled = false; button.textContent = "测试服务商"; } }
  }
  async function addProvider() {
    if (!state.settings && !await loadSettings()) return;
    const id = `provider_${Date.now().toString(36)}`;
    state.settings.webui.providers.push({ id, name: "新服务商", enabled: true, kind: "openai_images", base_url: "", generate_path: "/v1/images/generations", edit_path: "/v1/images/edits", model: "", api_key: "", custom_headers: "", timeout_seconds: 180, supports_text2img: true, supports_img2img: false, supports_negative_prompt: false, max_reference_images: 1, edit_request_format: "multipart", request_template: "", response_image_path: "" });
    state.selectedSettingsProviderId = id; renderSettingsProviders(); showNotice("已新增生图服务商，请填写配置后保存。", "success");
  }
  async function saveSettings() {
    if (!state.settings && !await loadSettings()) return;
    setError(els.settingsError, "正在保存设置…"); els.saveSettingsButton.disabled = true; els.saveSettingsButton.textContent = "保存中…";
    const webui = state.settings.webui; webui.history = { enabled: els.historyEnabled.checked, retain_reference_images: els.retainReferences.checked, max_records: Number(els.historyRecords.value), max_megabytes: Number(els.historyMegabytes.value) };
    try {
      await apiPost("settings/save", { settings_revision: webui.ui.settings_revision, base: { enabled: els.settingEnabled.checked, enable_llm_tool: els.settingTool.checked, max_concurrent_generations: Number(els.settingConcurrent.value) }, webui });
      await bootstrap(); await loadSettings(); setError(els.settingsError, ""); showNotice("设置已保存并生效。", "success");
    } catch (error) {
      const message = errorMessage(error, "设置保存失败"); setError(els.settingsError, message); showNotice(message, "error");
    } finally { els.saveSettingsButton.disabled = false; els.saveSettingsButton.textContent = "保存全部设置"; }
  }

  function dataUrlToFile(dataUrl, name) { const [head, encoded] = dataUrl.split(",", 2); const type = (head.match(/data:([^;]+)/) || [])[1] || "image/png"; const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0)); return new File([bytes], name, { type }); }
  async function exportSelected() { try { const result = await apiPost("gallery/export", { ids: Array.from(state.selectedIds) }); const client = await bridge(); await client.download(result.download_endpoint, {}, result.filename); showNotice("导出文件已开始下载。", "success"); } catch (error) { showNotice(errorMessage(error, "画廊导出失败"), "error"); } }
  async function deleteSelected() { if (!await confirmAction(`永久删除 ${state.selectedIds.size} 条生成记录及其结果图？`)) return; try { await apiPost("gallery/delete", { ids: Array.from(state.selectedIds) }); await loadGallery(); showNotice("所选生成记录已删除。", "success"); } catch (error) { showNotice(errorMessage(error, "生成记录删除失败"), "error"); } }
  async function useDataUrlAsReference(dataUrl, name) { try { const client = await bridge(); const uploaded = await client.upload("studio/reference/upload", dataUrlToFile(dataUrl, name)); state.references = [uploaded]; state.mode = "img2img"; document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === "img2img")); renderProviderChoices(); renderReferences(); closeDetail(); switchView("generate"); setError(els.generationError, "已将当前成图作为新的图生图参考图。它不会被当作历史原始参考图。"); } catch (error) { setError(els.generationError, errorMessage(error, "添加参考图失败")); } }
  function confirmAction(message) { return new Promise((resolve) => { const dialog = $("confirmDialog"); const cancel = $("confirmCancel"); const accept = $("confirmAccept"); $("confirmMessage").textContent = message; dialog.classList.remove("is-hidden"); els.scrim.classList.remove("is-hidden"); accept.focus(); const onKeydown = (event) => { if (event.key === "Escape") finish(false); }; const finish = (value) => { dialog.classList.add("is-hidden"); if (!els.detailDrawer.classList.contains("is-open")) els.scrim.classList.add("is-hidden"); cancel.removeEventListener("click", onCancel); accept.removeEventListener("click", onAccept); document.removeEventListener("keydown", onKeydown); activeConfirmation = null; resolve(value); }; const onCancel = () => finish(false); const onAccept = () => finish(true); activeConfirmation = finish; cancel.addEventListener("click", onCancel); accept.addEventListener("click", onAccept); document.addEventListener("keydown", onKeydown); }); }

  function bindEvents() {
    if (eventsBound) return;
    eventsBound = true;
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
    document.querySelectorAll(".segment").forEach((button) => button.addEventListener("click", () => { state.mode = button.dataset.mode; document.querySelectorAll(".segment").forEach((item) => item.classList.toggle("is-active", item === button)); renderProviderChoices(); }));
    els.referenceUpload.addEventListener("change", async () => { try { await uploadReferences(els.referenceUpload.files); } catch (error) { setError(els.generationError, errorMessage(error, "上传参考图失败")); } finally { els.referenceUpload.value = ""; } });
    els.generationForm.addEventListener("submit", generate); $("galleryRefresh").addEventListener("click", () => void loadGallery()); els.gallerySearch.addEventListener("change", () => void loadGallery()); els.galleryProvider.addEventListener("change", () => void loadGallery()); els.galleryMode.addEventListener("change", () => void loadGallery());
    $("selectAllButton").addEventListener("click", () => { state.galleryItems.forEach((item) => state.selectedIds.add(item.id)); els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => { input.checked = true; }); updateSelection(); }); $("exportButton").addEventListener("click", () => void exportSelected()); $("deleteButton").addEventListener("click", () => void deleteSelected());
    $("closeDrawer").addEventListener("click", closeDetail); $("closeImagePreview").addEventListener("click", closeImagePreview); els.imagePreview.querySelector("[data-close-image-preview]").addEventListener("click", closeImagePreview); els.previewImage.addEventListener("click", closeImagePreview); els.scrim.addEventListener("click", () => { if (activeConfirmation) activeConfirmation(false); else closeDetail(); }); els.addProviderButton.addEventListener("click", () => void addProvider()); els.saveSettingsButton.addEventListener("click", () => void saveSettings());
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
