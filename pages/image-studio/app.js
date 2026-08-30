(function () {
  "use strict";

  const state = {
    view: "generate", mode: "text2img", providers: [], selectedProviderId: "", references: [],
    resultImages: [], galleryItems: [], selectedIds: new Set(), settings: null, selectedSettingsProviderId: "",
  };
  let activeConfirmation = null;
  const $ = (id) => document.getElementById(id);
  const els = {
    pageTitle: $("pageTitle"), pageSubtitle: $("pageSubtitle"), runtimeStatus: $("runtimeStatus"), providerStatus: $("providerStatus"),
    providerChoices: $("providerChoices"), referenceField: $("referenceField"), referenceUpload: $("referenceUpload"), referenceStrip: $("referenceStrip"),
    generationForm: $("generationForm"), prompt: $("prompt"), negativePrompt: $("negativePrompt"), model: $("model"), size: $("size"), count: $("count"), parameters: $("parameters"), generationError: $("generationError"), generateButton: $("generateButton"), resultEmpty: $("resultEmpty"), resultGrid: $("resultGrid"), resultMeta: $("resultMeta"),
    galleryGrid: $("galleryGrid"), galleryEmpty: $("galleryEmpty"), gallerySearch: $("gallerySearch"), galleryProvider: $("galleryProvider"), galleryMode: $("galleryMode"), selectionBar: $("selectionBar"), selectionCount: $("selectionCount"),
    detailDrawer: $("detailDrawer"), drawerBody: $("drawerBody"), detailDate: $("detailDate"), scrim: $("scrim"),
    settingEnabled: $("settingEnabled"), settingTool: $("settingTool"), settingConcurrent: $("settingConcurrent"), historyEnabled: $("historyEnabled"), retainReferences: $("retainReferences"), historyRecords: $("historyRecords"), historyMegabytes: $("historyMegabytes"), settingsProviderList: $("settingsProviderList"), providerForm: $("providerForm"), settingsError: $("settingsError"),
  };

  async function bridge() {
    const deadline = Date.now() + 5000;
    while (!window.AstrBotPluginPage && Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 25));
    }
    if (!window.AstrBotPluginPage) throw new Error("AstrBot 页面桥接尚未加载，请刷新页面后重试。");
    return window.AstrBotPluginPage.ready();
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

  function switchView(view) {
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
    document.querySelectorAll(".view").forEach((item) => item.classList.toggle("is-active", item.id === `${view}View`));
    const labels = { generate: ["生图", "选择模式和 Provider 后开始创作"], gallery: ["画廊", "搜索、筛选、复现或导出历史生成记录"], settings: ["设置", "管理运行策略、历史和 Providers"], };
    els.pageTitle.textContent = labels[view][0]; els.pageSubtitle.textContent = labels[view][1];
    if (view === "gallery") loadGallery();
    if (view === "settings") loadSettings();
  }

  function renderProviderChoices() {
    const available = providerForMode();
    if (!available.some((item) => item.id === state.selectedProviderId)) state.selectedProviderId = available[0]?.id || "";
    els.providerChoices.innerHTML = available.length ? available.map((item) => `<button class="provider-chip ${item.id === state.selectedProviderId ? "is-active" : ""}" type="button" data-provider-id="${escape(item.id)}">${escape(item.name)}<small> ${escape(item.model)}</small></button>`).join("") : '<div class="provider-empty">当前模式没有可用 Provider，请先前往设置完成配置。</div>';
    els.providerChoices.querySelectorAll("[data-provider-id]").forEach((button) => button.addEventListener("click", () => { state.selectedProviderId = button.dataset.providerId; renderProviderChoices(); renderGenerationForm(); }));
    renderGenerationForm();
  }

  function renderGenerationForm() {
    const provider = selectedProvider();
    const supportsRefs = state.mode === "img2img";
    els.referenceField.classList.toggle("is-hidden", !supportsRefs);
    els.generateButton.disabled = !provider;
    els.providerStatus.textContent = provider ? `${provider.name} · ${provider.kind}` : "未配置 Provider";
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
    els.runtimeStatus.textContent = payload.enabled ? `${state.providers.length} 个 Provider 已加载` : "Image Studio 已关闭";
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
    if (!provider) { setError(els.generationError, "请先配置支持当前模式的 Provider"); return; }
    if (state.mode === "img2img" && !state.references.length) { setError(els.generationError, "图生图需要至少一张参考图"); return; }
    els.generateButton.disabled = true; els.generateButton.textContent = "生成中";
    try {
      const result = await apiPost("studio/generate", { mode: state.mode, provider_id: provider.id, prompt: els.prompt.value, negative_prompt: els.negativePrompt.value, model: els.model.value, size: els.size.value, count: Number(els.count.value), parameters, reference_ids: state.references.map((item) => item.id) });
      state.resultImages = result.images || []; state.references = []; renderReferences();
      els.resultEmpty.classList.toggle("is-hidden", state.resultImages.length > 0); els.resultGrid.innerHTML = state.resultImages.map((image, index) => `<div class="result-card"><img src="${image.data_url}" alt="生成结果" /><button class="quiet-button" data-result-reference="${index}" type="button">用作参考图</button></div>`).join("");
      els.resultGrid.querySelectorAll("[data-result-reference]").forEach((button) => button.addEventListener("click", () => useDataUrlAsReference(state.resultImages[Number(button.dataset.resultReference)].data_url, "generated-reference.png")));
      els.resultMeta.textContent = `${result.provider_name} · ${result.model} · ${(result.elapsed_ms / 1000).toFixed(1)} 秒${result.generation_id ? " · 已保存到画廊" : " · 历史未保留"}`;
    } catch (error) { setError(els.generationError, error.message || "生成失败"); }
    finally { els.generateButton.disabled = false; els.generateButton.textContent = "生成图片"; }
  }

  async function loadGallery() {
    const payload = await apiGet("gallery/list", { query: els.gallerySearch.value, provider_id: els.galleryProvider.value, mode: els.galleryMode.value });
    state.galleryItems = payload.items || []; state.selectedIds.clear(); renderGallery(payload);
  }

  function renderGallery(payload) {
    const currentProvider = els.galleryProvider.value; els.galleryProvider.innerHTML = '<option value="">全部 Provider</option>' + (payload.filters?.providers || []).map((item) => `<option value="${escape(item.id)}">${escape(item.name)}</option>`).join(""); els.galleryProvider.value = currentProvider;
    els.galleryEmpty.classList.toggle("is-hidden", state.galleryItems.length > 0); els.galleryGrid.innerHTML = state.galleryItems.map((item) => `<article class="gallery-card" data-gallery-id="${item.id}"><div class="gallery-image-wrap"><img src="${item.thumbnail_data_url}" alt="${escape(item.prompt_preview)}" /><input type="checkbox" data-select-id="${item.id}" aria-label="选择生成记录" /></div><div class="gallery-info"><strong>${escape(item.provider_name)} · ${escape(item.model)}</strong><p>${escape(item.prompt_preview)}</p><div class="gallery-meta"><span>${item.mode === "img2img" ? "图生图" : "文生图"}</span><span>${formatDate(item.created_at)}</span></div></div></article>`).join("");
    els.galleryGrid.querySelectorAll("[data-gallery-id]").forEach((card) => card.addEventListener("click", (event) => { if (event.target.matches("input")) return; openDetail(card.dataset.galleryId); }));
    els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => input.addEventListener("change", () => { input.checked ? state.selectedIds.add(input.dataset.selectId) : state.selectedIds.delete(input.dataset.selectId); updateSelection(); }));
    updateSelection();
  }

  function updateSelection() { els.selectionBar.classList.toggle("is-hidden", state.selectedIds.size === 0); els.selectionCount.textContent = `已选 ${state.selectedIds.size} 项`; }

  async function openDetail(id) {
    const detail = await apiGet(`gallery/detail/${id}`); els.detailDate.textContent = formatDate(detail.created_at);
    const primary = detail.images?.[0]; const refs = detail.references || [];
    els.drawerBody.innerHTML = `${primary ? `<img class="detail-image" src="${primary.data_url}" alt="生成图片" />` : ""}<div class="detail-block"><h3>提示词</h3><pre>${escape(detail.original_prompt)}</pre></div><div class="detail-block"><h3>请求参数</h3><pre>${escape(JSON.stringify(detail.parameters, null, 2))}</pre></div><div class="detail-block"><h3>信息</h3><pre>${escape(JSON.stringify({ provider: detail.provider_name, model: detail.model, mode: detail.mode, source: detail.source, elapsed_ms: detail.elapsed_ms }, null, 2))}</pre></div><div class="detail-block"><h3>参考图</h3><div class="detail-references">${refs.length ? refs.map((item) => item.available ? `<article class="detail-reference"><img src="${item.data_url}" alt="${escape(item.filename)}" /><div>${escape(item.filename)}<br>${formatBytes(item.size_bytes)}</div><button class="danger-button" data-reference-delete="${item.id}" type="button">删除参考图</button></article>` : `<article class="detail-reference"><div>参考图已删除</div></article>`).join("") : "<span>该记录没有保留参考图</span>"}</div></div><div class="detail-block"><button class="primary-button" data-reproduce="${detail.id}" type="button">复现参数</button>${primary ? ' <button class="quiet-button" data-output-reference="1" type="button">将当前成图用作新参考图</button>' : ""}</div>`;
    els.drawerBody.querySelectorAll("[data-reference-delete]").forEach((button) => button.addEventListener("click", async () => { if (!await confirmAction("删除此参考图？生成结果和参数不会删除。")) return; await apiPost("gallery/reference/delete", { reference_id: button.dataset.referenceDelete }); openDetail(id); }));
    els.drawerBody.querySelector("[data-reproduce]")?.addEventListener("click", () => reproduce(id));
    els.drawerBody.querySelector("[data-output-reference]")?.addEventListener("click", () => useDataUrlAsReference(primary.data_url, "gallery-output-reference.png"));
    els.detailDrawer.classList.add("is-open"); els.detailDrawer.setAttribute("aria-hidden", "false"); els.scrim.classList.remove("is-hidden");
  }

  function closeDetail() { els.detailDrawer.classList.remove("is-open"); els.detailDrawer.setAttribute("aria-hidden", "true"); if (!activeConfirmation) els.scrim.classList.add("is-hidden"); }

  async function reproduce(id) {
    const draft = await apiPost(`gallery/reproduce/${id}`, {}); state.mode = draft.mode || "text2img"; state.selectedProviderId = draft.provider_id || ""; state.references = draft.references || [];
    els.prompt.value = draft.prompt || ""; els.negativePrompt.value = draft.negative_prompt || ""; els.model.value = draft.model || ""; els.size.value = draft.size || ""; els.count.value = draft.count || 1; els.parameters.value = JSON.stringify(draft.parameters || {}, null, 2);
    document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === state.mode)); renderProviderChoices(); closeDetail(); switchView("generate");
    if (draft.notice) setError(els.generationError, draft.notice);
  }

  async function loadSettings() {
    const payload = await apiGet("settings/get"); state.settings = payload;
    els.settingEnabled.checked = !!payload.base.enabled; els.settingTool.checked = !!payload.base.enable_llm_tool; els.settingConcurrent.value = payload.base.max_concurrent_generations;
    const history = payload.webui.history; els.historyEnabled.checked = !!history.enabled; els.retainReferences.checked = !!history.retain_reference_images; els.historyRecords.value = history.max_records; els.historyMegabytes.value = history.max_megabytes;
    if (!state.selectedSettingsProviderId) state.selectedSettingsProviderId = payload.webui.providers[0]?.id || ""; renderSettingsProviders();
  }

  function currentSettingsProvider() { return state.settings?.webui.providers.find((item) => item.id === state.selectedSettingsProviderId) || null; }
  function renderSettingsProviders() {
    const providers = state.settings?.webui.providers || []; els.settingsProviderList.innerHTML = providers.length ? providers.map((item) => `<button class="provider-row ${item.id === state.selectedSettingsProviderId ? "is-active" : ""}" type="button" data-settings-provider="${escape(item.id)}"><strong>${escape(item.name || item.id)}</strong><span>${item.enabled ? "启用" : "停用"}</span></button>`).join("") : '<div class="provider-empty">尚未添加 Provider</div>';
    els.settingsProviderList.querySelectorAll("[data-settings-provider]").forEach((button) => button.addEventListener("click", () => { state.selectedSettingsProviderId = button.dataset.settingsProvider; renderSettingsProviders(); }));
    renderProviderEditor();
  }
  function renderProviderEditor() {
    const provider = currentSettingsProvider(); if (!provider) { els.providerForm.innerHTML = '<div class="provider-empty">选择或新增 Provider 后编辑详细配置。</div>'; return; }
    const kinds = [["openai_images", "OpenAI Images"], ["gemini", "Gemini"], ["nai_direct", "NAI 直连"], ["custom_json", "自定义 JSON"]];
    els.providerForm.innerHTML = `<h3>${escape(provider.name || "Provider")}</h3>${field("id", "ID", provider.id)}${field("name", "名称", provider.name)}${selectField("kind", "类型", provider.kind, kinds)}${field("base_url", "Base URL", provider.base_url)}${field("model", "默认模型", provider.model)}${field("generate_path", "文生图路径", provider.generate_path)}${field("edit_path", "图生图路径", provider.edit_path)}${field("api_key", "API Key", provider.api_key)}${field("timeout_seconds", "超时秒数", provider.timeout_seconds, "number")}${selectField("edit_request_format", "图生图格式", provider.edit_request_format, [["multipart", "multipart"], ["json_data_url", "JSON data URL"]])}${toggleField("enabled", "启用", provider.enabled)}${toggleField("supports_text2img", "支持文生图", provider.supports_text2img)}${toggleField("supports_img2img", "支持图生图", provider.supports_img2img)}${field("max_reference_images", "最大参考图数", provider.max_reference_images, "number")}${textAreaField("custom_headers", "自定义请求头", provider.custom_headers)}${textAreaField("request_template", "自定义 JSON 请求模板", provider.request_template)}${field("response_image_path", "响应图片路径", provider.response_image_path)}<div class="provider-editor-actions"><button class="danger-button" id="removeProviderButton" type="button">删除 Provider</button><button class="quiet-button" id="testProviderButton" type="button">测试 Provider</button></div>`;
    els.providerForm.querySelectorAll("[data-provider-field]").forEach((input) => input.addEventListener("input", () => updateProviderField(input))); els.providerForm.querySelectorAll("[data-provider-field]").forEach((input) => input.addEventListener("change", () => updateProviderField(input)));
    $("removeProviderButton")?.addEventListener("click", async () => { if (!await confirmAction("删除此 Provider？历史记录不会删除。")) return; state.settings.webui.providers = state.settings.webui.providers.filter((item) => item.id !== provider.id); state.selectedSettingsProviderId = state.settings.webui.providers[0]?.id || ""; renderSettingsProviders(); });
    $("testProviderButton")?.addEventListener("click", () => testProvider(provider));
  }
  function field(key, label, value, type = "text") { return `<div class="field"><label>${label}</label><input data-provider-field="${key}" type="${type}" value="${escape(value)}" /></div>`; }
  function textAreaField(key, label, value) { return `<div class="field field-wide"><label>${label}</label><textarea data-provider-field="${key}" rows="3">${escape(value)}</textarea></div>`; }
  function selectField(key, label, value, options) { return `<div class="field"><label>${label}</label><select data-provider-field="${key}">${options.map(([id, name]) => `<option value="${id}" ${id === value ? "selected" : ""}>${name}</option>`).join("")}</select></div>`; }
  function toggleField(key, label, value) { return `<div class="toggle-row"><label>${label}</label><label class="toggle-control"><input data-provider-field="${key}" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`; }
  function updateProviderField(input) { const provider = currentSettingsProvider(); if (!provider) return; provider[input.dataset.providerField] = input.type === "checkbox" ? input.checked : input.value; if (input.dataset.providerField === "id") state.selectedSettingsProviderId = input.value; }
  async function testProvider(provider) { try { const payload = await apiPost("provider/test", { provider }); setError(els.settingsError, `Provider 测试成功，返回 ${payload.image_count} 张图片。`); } catch (error) { setError(els.settingsError, error.message || "测试失败"); } }
  function addProvider() { const id = `provider_${Date.now().toString(36)}`; state.settings.webui.providers.push({ id, name: "新 Provider", enabled: true, kind: "openai_images", base_url: "", generate_path: "/v1/images/generations", edit_path: "/v1/images/edits", model: "", api_key: "", custom_headers: "", timeout_seconds: 180, supports_text2img: true, supports_img2img: false, max_reference_images: 1, edit_request_format: "multipart", request_template: "", response_image_path: "" }); state.selectedSettingsProviderId = id; renderSettingsProviders(); }
  async function saveSettings() {
    setError(els.settingsError, ""); const webui = state.settings.webui; webui.history = { enabled: els.historyEnabled.checked, retain_reference_images: els.retainReferences.checked, max_records: Number(els.historyRecords.value), max_megabytes: Number(els.historyMegabytes.value) };
    try { await apiPost("settings/save", { settings_revision: webui.ui.settings_revision, base: { enabled: els.settingEnabled.checked, enable_llm_tool: els.settingTool.checked, max_concurrent_generations: Number(els.settingConcurrent.value) }, webui }); await bootstrap(); await loadSettings(); }
    catch (error) { setError(els.settingsError, error.message || "保存失败"); }
  }

  function dataUrlToFile(dataUrl, name) { const [head, encoded] = dataUrl.split(",", 2); const type = (head.match(/data:([^;]+)/) || [])[1] || "image/png"; const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0)); return new File([bytes], name, { type }); }
  async function exportSelected() { const result = await apiPost("gallery/export", { ids: Array.from(state.selectedIds) }); const client = await bridge(); await client.download(result.download_endpoint, {}, result.filename); }
  async function deleteSelected() { if (!await confirmAction(`永久删除 ${state.selectedIds.size} 条生成记录及其结果图？`)) return; await apiPost("gallery/delete", { ids: Array.from(state.selectedIds) }); await loadGallery(); }
  async function useDataUrlAsReference(dataUrl, name) { try { const client = await bridge(); const uploaded = await client.upload("studio/reference/upload", dataUrlToFile(dataUrl, name)); state.references = [uploaded]; state.mode = "img2img"; document.querySelectorAll(".segment").forEach((button) => button.classList.toggle("is-active", button.dataset.mode === "img2img")); renderProviderChoices(); renderReferences(); closeDetail(); switchView("generate"); setError(els.generationError, "已将当前成图作为新的图生图参考图。它不会被当作历史原始参考图。"); } catch (error) { setError(els.generationError, error.message || "添加参考图失败"); } }
  function confirmAction(message) { return new Promise((resolve) => { const dialog = $("confirmDialog"); const cancel = $("confirmCancel"); const accept = $("confirmAccept"); $("confirmMessage").textContent = message; dialog.classList.remove("is-hidden"); els.scrim.classList.remove("is-hidden"); accept.focus(); const onKeydown = (event) => { if (event.key === "Escape") finish(false); }; const finish = (value) => { dialog.classList.add("is-hidden"); if (!els.detailDrawer.classList.contains("is-open")) els.scrim.classList.add("is-hidden"); cancel.removeEventListener("click", onCancel); accept.removeEventListener("click", onAccept); document.removeEventListener("keydown", onKeydown); activeConfirmation = null; resolve(value); }; const onCancel = () => finish(false); const onAccept = () => finish(true); activeConfirmation = finish; cancel.addEventListener("click", onCancel); accept.addEventListener("click", onAccept); document.addEventListener("keydown", onKeydown); }); }

  function bindEvents() {
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
    document.querySelectorAll(".segment").forEach((button) => button.addEventListener("click", () => { state.mode = button.dataset.mode; document.querySelectorAll(".segment").forEach((item) => item.classList.toggle("is-active", item === button)); renderProviderChoices(); }));
    els.referenceUpload.addEventListener("change", async () => { try { await uploadReferences(els.referenceUpload.files); } catch (error) { setError(els.generationError, error.message || "上传参考图失败"); } finally { els.referenceUpload.value = ""; } });
    els.generationForm.addEventListener("submit", generate); $("galleryRefresh").addEventListener("click", loadGallery); els.gallerySearch.addEventListener("change", loadGallery); els.galleryProvider.addEventListener("change", loadGallery); els.galleryMode.addEventListener("change", loadGallery);
    $("selectAllButton").addEventListener("click", () => { state.galleryItems.forEach((item) => state.selectedIds.add(item.id)); els.galleryGrid.querySelectorAll("[data-select-id]").forEach((input) => { input.checked = true; }); updateSelection(); }); $("exportButton").addEventListener("click", exportSelected); $("deleteButton").addEventListener("click", deleteSelected);
    $("closeDrawer").addEventListener("click", closeDetail); els.scrim.addEventListener("click", () => { if (activeConfirmation) activeConfirmation(false); else closeDetail(); }); $("addProviderButton").addEventListener("click", addProvider); $("saveSettingsButton").addEventListener("click", saveSettings);
  }

  async function start() { bindEvents(); try { await bootstrap(); } catch (error) { els.runtimeStatus.textContent = error.message || "初始化失败"; } }
  start();
})();
