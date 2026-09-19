(function () {
  "use strict";

  // Settings own their editor selection, draft baseline and save/recovery lifecycle.
  // Other views are reached through explicit callbacks, never a shared app state.
  window.ImageStudioSettings = function (hooks) {
    const { escape, apiGet, apiPost, showNotice, errorMessage, formatDate, formatBytes, setError, confirmAction, syncPageScrollLock, schemaParameterTitle, schemaParameterLabel, configuredReferenceLimit, referenceLimitForModel, effectiveModelParameters, modelParameterMatches, bootstrap, closeDetail, switchView, library } = hooks;
    const $ = id => document.getElementById(id);
    const state = { settings: null, selectedSettingsProviderId: "", selectedSettingsModelId: "", modelEditorTab: "model", editingToolParameter: "", editingToolDefaultChoices: [] };
    const els = Object.fromEntries(["addModelButton", "addProviderButton", "agentImageReturnMode", "agentPreviewMaxEdge", "agentPreviewQuality", "filter", "find", "flatMap", "historyEnabled", "historyMegabytes", "historyRecords", "length", "map", "modelForm", "newModelChoice", "newModelChoices", "parameterDialog", "providerForm", "push", "recordInvocationIdentity", "retainReferences", "runDeepMaintenanceButton", "runMaintenanceButton", "saveSettingsButton", "settingPageDefaultImageModel", "settingPageDefaultTextModel", "settingTool", "settingToolDefaultImageModel", "settingToolDefaultTextModel", "settingsError", "settingsModelList", "settingsProviderList", "some", "storageHealthAssets", "storageHealthCheckedAt", "storageHealthDuration", "storageHealthErrors", "storageHealthGenerations", "storageHealthLeases", "storageHealthSize", "storageHealthStatus", "toolParameterChoices", "toolParameterDefault", "toolParameterDefaultChoice", "toolParameterDefaultHint", "toolParameterDescription", "toolParameterExposed"].map(id => [id, $(id)]));
    const MODEL_DEFAULT_CHOICE = "__model_default__";
    let bound = false;
    let settingsLoadPromise = null;
    let settingsBaseline = "";
    let savedSettingsPayload = null;
    let settingsReadPending = false;
    let settingsReadPromise = null;
    let settingsBootstrapPending = false;
    let settingsSaving = false;
    let settingsNavigationPending = false;
    let storageRetention = null;
    let novelaiModels = [];
    let gallerySortDraft = "created";

    async function leaveSettings(view) {
      if (settingsNavigationPending) return;
      if (settingsSaving) { showNotice("正在保存设置，请稍候再切换页面。", "info"); return; }
      settingsNavigationPending = true;
      try {
        const discard = await library.openModal("有未保存的设置", "<p>离开设置页面会放弃本次尚未保存的调整，包括主题与显示设置。</p>", [
          { label: "放弃设置", danger: true, id: "discardSettingsButton", action: () => true },
          { label: "留在设置页面", primary: true, id: "staySettingsButton", action: () => false },
        ], { focus: "staySettingsButton" });
        if (!discard) return;
        // Restore every editor from the last successful save, without requiring
        // another network request or losing drafts when choosing to stay.
        if (savedSettingsPayload && !await loadSettings(true, structuredClone(savedSettingsPayload))) return;
        window.ImageStudioAppearance.discard();
        gallerySortDraft = hooks.getGallerySort();
        if ($("gallerySort")) { $("gallerySort").value = gallerySortDraft; window.ImageStudioSelect?.refresh($("gallerySort")); }
        updateSettingsDirty();
        hooks.showView(view);
      } finally { settingsNavigationPending = false; }
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
      els.storageHealthLeases.textContent = String(Number(stats.active_leases || 0)); els.storageHealthGenerations.textContent = String(Number(stats.generations || 0)); els.storageHealthSize.textContent = formatBytes(stats.disk?.file_bytes ?? stats.size_bytes ?? 0);
      $("storageHealthAllocated").textContent = stats.disk ? formatBytes(stats.disk.allocated_bytes || 0) : "-";
      $("storageHealthReusable").textContent = formatBytes(stats.disk?.database?.reusable_bytes || 0);
      const categories = [["originals", "画廊原图与参考图"], ["thumbnails", "预览图"], ["comfy_inputs", "ComfyUI 输入缓存"], ["comfy_outputs", "ComfyUI 输出缓存"], ["comfy_blobs", "ComfyUI 共享图片"], ["database", "数据库"], ["backups", "升级备份"], ["temporary", "其他临时文件"], ["other", "配置及其他文件"]];
      $("storageHealthBreakdown").innerHTML = stats.disk ? categories.map(([key, label]) => `<div><span>${escape(label)}</span><strong>${escape(formatBytes(stats.disk.categories?.[key]?.file_bytes || 0))}</strong></div>`).join("") : "";
      const repaired = report?.repaired || {};
      const repairs = [["expired_leases", "到期保留记录"], ["expired_import_batches", "过期导入批次"], ["broken_assets", "不可用原图"], ["rebuilt_thumbnails", "重建预览"], ["unreferenced_assets", "无引用图片"], ["orphan_files", "无引用文件"], ["stale_temporary_files", "过期临时文件"], ["comfy_files", "ComfyUI 缓存文件"], ["unused_payloads", "无引用快照"], ["backups_removed", "旧升级备份"]].filter(([key]) => Number(repaired[key]) > 0).map(([key, label]) => `${label} ${Number(repaired[key])} 项`);
      if (report?.database?.compacted) repairs.push(`数据库缩减 ${formatBytes(report.database.bytes_reclaimed)}`);
      else if (report?.deep && report?.database?.reason === "comfy_tasks_pending") repairs.push("有待处理的 ComfyUI 任务，暂缓数据库压缩");
      else if (report?.deep && report?.database?.reason === "insufficient_working_space") repairs.push("可用磁盘空间不足，暂缓数据库压缩");
      else if (report?.deep && report?.database?.reason === "database_busy") repairs.push("数据库正在使用，暂缓压缩");
      $("storageHealthRepaired").textContent = repairs.length ? `最近维护：${repairs.join("；")}。` : "";
      $("storageHealthRepaired").classList.toggle("is-hidden", !repairs.length);
      els.storageHealthErrors.textContent = errors.length ? errors.join("；") : "暂无异常。";
      if (report?.retention) storageRetention = report.retention;
      if (Array.isArray(report?.external_sources)) hooks.externalSources().ingest(report.external_sources);
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
      if (deep && !await confirmAction("深度检查会重新计算全部原图哈希，并在数据库空闲空间较多时压缩数据库文件。历史较多时可能耗时较长，期间数据库操作可能需要等待。继续执行？")) return;
      els.runMaintenanceButton.disabled = true; els.runDeepMaintenanceButton.disabled = true; els.storageHealthStatus.textContent = "检查中";
      try { const report = await apiPost("storage/maintenance", { deep: !!deep }); renderStorageHealth(report); await loadStorageHealth(); showNotice(deep ? "存储深度检查已完成。" : "存储检查已完成。", report.status === "error" ? "error" : "success"); }
      catch (error) { showNotice(errorMessage(error, "存储检查失败"), "error"); await loadStorageHealth(); }
      finally { els.runMaintenanceButton.disabled = false; els.runDeepMaintenanceButton.disabled = false; }
    }

    async function loadSettings(force = false, suppliedPayload = null) {
      if (state.settings && !force) {
        if (settingsReadPending) {
          try { await rereadConfirmedSettings(); }
          catch (error) { showNotice(`设置已保存，但重新读取失败：${errorMessage(error, "请稍后重试")}`, "info"); }
        }
        return true;
      }
      if (settingsLoadPromise) return settingsLoadPromise;
      els.addProviderButton.disabled = true; els.addModelButton.disabled = true; els.saveSettingsButton.disabled = true;
      setError(els.settingsError, "正在读取设置…");
      settingsLoadPromise = (async () => {
        try {
          const payload = suppliedPayload || await apiGet("settings/get");
          if (!payload?.base || !payload?.webui || !Array.isArray(payload.webui.providers)) throw new Error("设置接口返回的数据格式无效");
          normalizeSettingsModelDefaults(payload);
          savedSettingsPayload = structuredClone(payload);
          state.settings = payload;
          if (Array.isArray(payload.novelai_models)) novelaiModels = payload.novelai_models;
          els.settingTool.checked = !!payload.base.enable_llm_tool;
          const llmPolicy = payload.webui.llm_policy || {}; const assetPolicy = payload.webui.asset_policy || {}; els.agentImageReturnMode.value = ["asset", "preview", "original"].includes(llmPolicy.image_return_mode) ? llmPolicy.image_return_mode : "preview"; els.agentPreviewMaxEdge.value = Number(assetPolicy.preview_max_edge || 768); els.agentPreviewQuality.value = Number(assetPolicy.preview_quality || 80); syncAgentImageSettings();
          const history = payload.webui.history; els.historyEnabled.checked = !!history.enabled; els.retainReferences.checked = !!history.retain_reference_images; els.recordInvocationIdentity.checked = !!history.record_invocation_identity; els.historyRecords.value = history.max_records; els.historyMegabytes.value = history.max_megabytes;
          refreshSettingsDefaultModels(payload.webui.generation_defaults || {});
          if (!payload.webui.providers.some((item) => item.id === state.selectedSettingsProviderId)) state.selectedSettingsProviderId = payload.webui.providers[0]?.id || "";
          state.selectedSettingsModelId = "";
          renderSettingsProviders();
          hooks.externalSources().settingsLoaded();
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
          els.addProviderButton.disabled = false; els.addModelButton.disabled = false; els.saveSettingsButton.disabled = settingsSaving;
        }
      })();
      const loaded = await settingsLoadPromise;
      settingsLoadPromise = null;
      return loaded;
    }

    const PROVIDER_KINDS = [["openai_images", "OpenAI Images"], ["gemini", "Gemini 图片输出"], ["novelai_official", "NovelAI 官方"], ["nai_direct", "NAI 第三方 GET（nai.sta1n.cn）"], ["comfyui", "ComfyUI 工作流"], ["custom_json", "自定义 JSON"]];
    const NAI_MODELS = [
      { id: "nai-diffusion-4-5-full", name: "NAI V4.5 完整版" },
      { id: "nai-diffusion-5-full", name: "NAI V5 完整版" },
    ];
    const NOVELAI_OFFICIAL_MODELS = [
      { id: "nai-diffusion-4-5-full", name: "NovelAI V4.5 完整版", supports_text2img: true, supports_img2img: true, supports_negative_prompt: true, max_reference_images: 8, capability_source: "builtin" },
      { id: "nai-diffusion-4-5-curated", name: "NovelAI V4.5 精选版", supports_text2img: true, supports_img2img: true, supports_negative_prompt: true, max_reference_images: 8, capability_source: "builtin" },
      { id: "nai-diffusion-5-full", name: "NovelAI V5 完整版", supports_text2img: true, supports_img2img: true, supports_negative_prompt: true, max_reference_images: 2, capability_source: "builtin" },
      { id: "nai-diffusion-5-curated", name: "NovelAI V5 精选版", supports_text2img: true, supports_img2img: true, supports_negative_prompt: true, max_reference_images: 2, capability_source: "builtin" },
    ];
    function builtinProviderModels(provider) { return provider?.kind === "novelai_official" ? novelaiModels.length ? novelaiModels : NOVELAI_OFFICIAL_MODELS : provider?.kind === "nai_direct" ? NAI_MODELS : null; }
    const PROVIDER_DEFAULTS = {
      comfyui: { base_url: "http://127.0.0.1:8188", generate_path: "/prompt", edit_path: "/prompt", models_path: "/object_info", request_template: "", response_image_path: "", max_concurrent_generations: 1, timeout_seconds: 600 },
      openai_images: { base_url: "https://api.openai.com/v1", generate_path: "/images/generations", edit_path: "/images/edits", models_path: "/models", edit_request_format: "multipart", request_template: "", response_image_path: "", max_concurrent_generations: 2 },
      gemini: { base_url: "https://generativelanguage.googleapis.com", generate_path: "/v1beta/models/{model}:generateContent", edit_path: "/v1beta/models/{model}:generateContent", models_path: "/v1beta/models", edit_request_format: "json_data_url", request_template: "", response_image_path: "", max_concurrent_generations: 2 },
      nai_direct: { base_url: "https://nai.sta1n.cn", generate_path: "/generate", edit_path: "", models_path: "/models", edit_request_format: "json_data_url", request_template: "", response_image_path: "", max_concurrent_generations: 2 },
      novelai_official: { base_url: "https://image.novelai.net", generate_path: "/ai/generate-image", edit_path: "/ai/generate-image", models_path: "", edit_request_format: "json_data_url", request_template: "", response_image_path: "", max_concurrent_generations: 1 },
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
    function modelPreset(kind, modelId) {
      if (kind === "comfyui") return hooks.comfyui().totalParameters();
      // Official defaults and supported fields come from the same catalog as
      // request validation, including the different V4.5 / V5 capabilities.
      if (kind === "novelai_official") return structuredClone(novelaiModels.find(model => model.id === modelId)?.parameters || novelaiModels[0]?.parameters || {});
      const preset = JSON.parse(JSON.stringify({ ...BATCH_PRESET, ...(MODEL_PRESETS[kind] || MODEL_PRESETS.custom_json) }));
      Object.entries(preset).forEach(([name, descriptor]) => { if (modelParameterMatches(name, descriptor, ["count", "n"])) descriptor.refill_from_history = false; });
      if (kind === "nai_direct") preset.style.record_in_history = false;
      return preset;
    }
    function ensureBatchConfig(model, provider) {
      const nai = provider.kind === "nai_direct";
      model.native_batch_size = nai ? 1 : Number(model.native_batch_size ?? 1);
      model.native_batch_size_source = nai ? "fixed" : model.native_batch_size_source || "default";
      model.max_concurrent_requests = Number(model.max_concurrent_requests ?? 8);
      model.parameters = model.parameters || {};
      if (provider.kind === "comfyui") model.parameters = hooks.comfyui().totalParameters(model.parameters);
      else if (!Object.entries(model.parameters).some(([name, descriptor]) => modelParameterMatches(name, descriptor, ["count", "n"]))) model.parameters.count = JSON.parse(JSON.stringify(BATCH_PRESET.count));
    }
    function currentSettingsProvider() { return state.settings?.webui.providers.find((item) => item.id === state.selectedSettingsProviderId) || null; }
    function settingsOrderRow(item, kind, selected, status, movable) {
      const name = item.name || item.id;
      const grip = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg>';
      return `<div class="provider-row settings-sortable-row ${selected ? "is-active" : ""}" data-settings-order-id="${escape(item.id)}" role="listitem"><button class="settings-row-select" type="button" data-settings-${kind}="${escape(item.id)}" data-sort-surface aria-pressed="${selected}" data-tooltip="${escape(name)}" data-tooltip-overflow="strong"><strong>${escape(name)}</strong><span>${escape(status)}</span></button><button class="settings-sort-handle" data-sort-handle type="button" aria-label="调整 ${escape(name)} 的顺序" data-tooltip="拖动排序，也可使用方向键或 Home / End"${movable ? "" : " disabled"}>${grip}</button></div>`;
    }

    function bindSettingsOrder(list, kind, provider = null) {
      const values = () => kind === "provider" ? state.settings?.webui.providers || [] : currentSettingsProvider() === provider ? provider.models || [] : [];
      const itemName = kind === "provider" ? "服务商" : provider?.kind === "comfyui" ? "工作流" : "模型";
      list.setAttribute("role", "list"); list.setAttribute("aria-label", `${itemName}顺序`);
      const canSort = () => hooks.getView() === "settings" && !settingsSaving && !settingsNavigationPending && !hooks.confirmationOpen() && !library.modalOpen() && els.parameterDialog.classList.contains("is-hidden");
      window.ImageStudioSortable.bind(list, {
        itemSelector: "[data-settings-order-id]", getId: item => item.dataset.settingsOrderId,
        getLabel: item => { const value = values().find(value => value.id === item.dataset.settingsOrderId); return value?.name || value?.id || itemName; },
        itemName, itemUnit: "项", isEnabled: canSort,
        onReorder: ids => {
          const items = values(), byId = new Map(items.map(item => [item.id, item]));
          if (!canSort() || ids.length !== items.length || new Set(ids).size !== ids.length || ids.some(id => !byId.has(id))) return;
          const reordered = ids.map(id => byId.get(id));
          if (kind === "provider") state.settings.webui.providers = reordered;
          else provider.models = reordered;
          // Only the order changes. Existing editor nodes, selected IDs and
          // typed values remain mounted; explicit default model refs stay put.
          refreshSettingsDefaultModels(); updateSettingsDirty();
        },
      });
    }

    function updateSettingsRowId(list, kind, id) {
      const row = list.querySelector(".provider-row.is-active");
      if (!row) return;
      row.dataset.settingsOrderId = id;
      const select = row.querySelector(`[data-settings-${kind}]`);
      if (select) select.dataset[kind === "provider" ? "settingsProvider" : "settingsModel"] = id;
    }

    function renderSettingsProviders() {
      const providers = state.settings?.webui.providers || []; els.settingsProviderList.innerHTML = providers.length ? providers.map(item => settingsOrderRow(item, "provider", item.id === state.selectedSettingsProviderId, item.enabled ? "启用" : "停用", providers.length > 1)).join("") : '<div class="provider-empty">尚未添加生图服务商</div>';
      els.settingsProviderList.querySelectorAll("[data-settings-provider]").forEach((button) => button.addEventListener("click", () => { state.selectedSettingsProviderId = button.dataset.settingsProvider; state.selectedSettingsModelId = ""; renderSettingsProviders(); }));
      bindSettingsOrder(els.settingsProviderList, "provider");
      renderProviderEditor();
      renderModelEditor();
      updateSettingsDirty();
    }
    function renderProviderEditor() {
      const provider = currentSettingsProvider(); if (!provider) { els.providerForm.innerHTML = '<div class="provider-empty">选择或新增生图服务商后编辑详细配置。</div>'; return; }
      const kind = provider.kind || "custom_json";
      const official = kind === "novelai_official";
      if (official) provider.max_concurrent_generations = 1;
      const credentialField = official ? `${field("api_key", "NovelAI 完整 API Token", provider.api_key)}<div class="field"><span class="field-hint">填写 NovelAI 账户设置中生成的完整 Persistent API Token，保留前缀，无需添加 Bearer。</span></div>` : kind === "nai_direct" ? `${field("api_key", "生图 Token（toUserId）", provider.api_key)}<div class="field"><span class="field-hint">填写在 nai.sta1n.cn 申请的 toUserId。</span></div>` : field("api_key", "接口密钥（API Key）", provider.api_key, "text", false, kind === "comfyui" ? "留空时不使用" : "");
      const headersField = kind === "nai_direct" ? "" : textAreaField("custom_headers", "自定义请求头（JSON 或每行一个 Header）", provider.custom_headers);
      const proxyField = field("proxy", "网络代理", provider.proxy || "", "text", false, "留空时不使用");
      const common = `${field("id", "ID", provider.id)}${field("name", "名称", provider.name)}${selectField("kind", "供应类型", kind, PROVIDER_KINDS)}${field("base_url", "接口地址（Base URL）", provider.base_url)}${credentialField}${field("timeout_seconds", "超时秒数", provider.timeout_seconds, "number")}${field("max_concurrent_generations", "Provider 最大并发", provider.max_concurrent_generations ?? 2, "number", official)}${official ? '<div class="field"><span class="field-hint">官方服务商当前固定串行生成，同时最多处理 1 个请求。</span></div>' : ""}${proxyField}${headersField}`;
      const typeFields = kind === "openai_images" ? `${field("generate_path", "文生图路径", provider.generate_path)}${field("edit_path", "图生图路径", provider.edit_path)}${field("models_path", "模型列表路径", provider.models_path || "/models")}${selectField("edit_request_format", "图生图请求格式", provider.edit_request_format, [["multipart", "multipart"], ["json_data_url", "JSON data URL"]])}` : kind === "gemini" ? `${field("generate_path", "generateContent 路径（支持 {model}）", provider.generate_path)}${field("models_path", "模型列表路径", provider.models_path || "/v1beta/models")}` : kind === "nai_direct" ? `${field("generate_path", "生成路径", provider.generate_path)}<div class="field field-wide"><span class="field-hint">第三方服务协议：GET /generate；Token 作为 token 查询参数发送。该类型不是 NovelAI 官方 API，且仅支持文生图。</span></div>` : `${field("generate_path", "文生图路径", provider.generate_path)}${field("edit_path", "图生图路径", provider.edit_path)}${field("models_path", "模型列表路径", provider.models_path || "/models")}${selectField("edit_request_format", "图生图请求格式", provider.edit_request_format, [["multipart", "multipart"], ["json_data_url", "JSON data URL"]])}${textAreaField("request_template", "请求 JSON 模板（可选）", provider.request_template)}${field("response_image_path", "响应图片路径（可选）", provider.response_image_path)}<div class="field field-wide"><span class="field-hint">模板可使用 {{prompt}}、{{model}}、{{size}}、{{count}} 和参数字段。</span></div>`;
      const officialFields = `${field("generate_path", "文生图路径", provider.generate_path)}${field("edit_path", "图生图路径", provider.edit_path)}<div class="field field-wide"><span class="field-hint">V4.5 支持单底图、精确角色 / 风格参考、Vibe、多角色与局部重绘；V5 支持单底图、多角色、透明背景与局部重绘。V5 精选版局部重绘使用 V4.5 精选版。</span></div>`;
      const discoveryButton = builtinProviderModels(provider) || kind === "comfyui" ? "" : '<button class="quiet-button" id="discoverModelsButton" type="button">获取模型</button>';
      const heading = `<div class="provider-editor-heading"><h3>${escape(provider.name || "生图服务商")}</h3><label class="toggle-control"><input data-provider-field="enabled" type="checkbox" aria-label="启用生图服务商" ${provider.enabled ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`;
      const comfyFields = '<div class="field field-wide"><span class="field-hint">连接自建 ComfyUI 原生服务。API Key 可留空；受保护的服务可填写请求头。下方每个工作流独立配置输入绑定及结果节点。</span></div>';
      els.providerForm.innerHTML = `${heading}${common}${kind === "comfyui" ? comfyFields : official ? officialFields : typeFields}<div class="provider-editor-actions"><button class="danger-button" id="removeProviderButton" type="button">删除服务商</button>${discoveryButton}</div>`;
      els.providerForm.querySelectorAll("[data-provider-field]").forEach((input) => input.addEventListener("input", () => updateProviderField(input))); els.providerForm.querySelectorAll("[data-provider-field]").forEach((input) => input.addEventListener("change", () => updateProviderField(input)));
      $("removeProviderButton")?.addEventListener("click", async () => { if (!await confirmAction("删除此生图服务商？历史记录不会删除。")) return; state.settings.webui.providers = state.settings.webui.providers.filter((item) => item.id !== provider.id); state.selectedSettingsProviderId = state.settings.webui.providers[0]?.id || ""; renderSettingsProviders(); showNotice("已从设置草稿中删除，保存全部设置后生效。", "success"); });
      $("discoverModelsButton")?.addEventListener("click", () => void discoverProviderModels(provider));
      window.ImageStudioSelect?.refresh(els.providerForm);
    }
    function field(key, label, value, type = "text", disabled = false, placeholder = "") { return `<div class="field"><label>${label}</label><input data-provider-field="${key}" type="${type}" value="${escape(value)}"${placeholder ? ` placeholder="${escape(placeholder)}"` : ""}${disabled ? " disabled" : ""} /></div>`; }
    function textAreaField(key, label, value) { return `<div class="field field-wide"><label>${label}</label><textarea data-provider-field="${key}" rows="3">${escape(value)}</textarea></div>`; }
    function selectField(key, label, value, options) { return `<div class="field"><label>${label}</label><select data-provider-field="${key}">${options.map(([id, name]) => `<option value="${id}" ${id === value ? "selected" : ""}>${name}</option>`).join("")}</select></div>`; }
    function updateProviderField(input) { const provider = currentSettingsProvider(); if (!provider) return; const key = input.dataset.providerField; const value = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value; if (key === "kind" && value !== provider.kind) { Object.assign(provider, providerDefaults(value)); provider.kind = value; state.selectedSettingsModelId = ""; renderProviderEditor(); renderModelEditor(); return; } provider[key] = value; if (key === "id") { state.selectedSettingsProviderId = input.value; updateSettingsRowId(els.settingsProviderList, "provider", input.value); const activeRow = els.settingsProviderList.querySelector(".provider-row.is-active"); if (activeRow && !provider.name) activeRow.querySelector("strong").textContent = input.value; } refreshSettingsDefaultModels(); }
    async function discoverProviderModels(provider) { const button = $("discoverModelsButton"); if (button) { button.disabled = true; button.textContent = "获取中…"; } try { const payload = await apiPost("provider/models", { provider }); provider.discovered_models = payload.models || []; for (const model of provider.models || []) { const discovered = provider.discovered_models.find((item) => item.id === model.id); if (discovered && model.native_batch_size_source !== "manual") { model.native_batch_size = Number(discovered.native_batch_size) || 1; model.native_batch_size_source = discovered.native_batch_size_source || "default"; } } renderModelEditor(); updateSettingsDirty(); showNotice(`已获取 ${provider.discovered_models.length} 个模型，可在新增模型时选择。`, "success"); } catch (error) { showNotice(errorMessage(error, "获取模型失败"), "error"); } finally { if (button) { button.disabled = false; button.textContent = "获取模型"; } } }
    function renderNewModelChoices(provider = currentSettingsProvider()) { const builtin = builtinProviderModels(provider), models = builtin || provider?.discovered_models || []; els.newModelChoices.innerHTML = models.map((item) => `<option value="${escape(item.id)}">${escape(item.name || item.id)}${item.capability_source === "unknown" ? " · 能力未知" : ""}</option>`).join(""); els.newModelChoice.value = ""; els.newModelChoice.placeholder = provider?.kind === "novelai_official" ? "选择 NovelAI 官方模型" : builtin ? "选择 NAI 模型或手动输入 ID" : "选择或输入模型 ID"; }

    function currentSettingsModel() { const provider = currentSettingsProvider(); return provider?.models?.find((item) => item.id === state.selectedSettingsModelId) || null; }
    function renderModelEditor() {
      const provider = currentSettingsProvider();
      const comfy = provider?.kind === "comfyui";
      els.addModelButton.textContent = comfy ? "新增工作流" : "新增模型";
      document.querySelector(".model-settings .section-heading h2").textContent = comfy ? "工作流配置" : "模型配置";
      document.querySelector(".model-settings .section-heading p").textContent = comfy ? "每份工作流独立保存执行图、可调整输入和结果节点。" : "同一服务商可以配置多个模型，每个模型独立声明模式、参考图和参数。";
      refreshSettingsDefaultModels();
      renderNewModelChoices(provider);
      if (comfy) els.newModelChoice.placeholder = "工作流 ID（可自动生成）";
      if (!provider) { els.settingsModelList.innerHTML = '<div class="provider-empty">请先选择服务商</div>'; els.modelForm.innerHTML = '<div class="provider-empty">选择服务商后配置模型能力。</div>'; return; }
      provider.models = Array.isArray(provider.models) ? provider.models : [];
      if (!provider.models.some((item) => item.id === state.selectedSettingsModelId)) state.selectedSettingsModelId = provider.models[0]?.id || "";
      els.settingsModelList.innerHTML = provider.models.length ? provider.models.map(item => settingsOrderRow(item, "model", item.id === state.selectedSettingsModelId, referenceLimitForModel(item) > 0 ? "图生图" : item.supports_text2img ? "文生图" : "未开放", provider.models.length > 1)).join("") : '<div class="provider-empty">该服务商尚未添加模型</div>';
      els.settingsModelList.querySelectorAll("[data-settings-model]").forEach((button) => button.addEventListener("click", () => { state.selectedSettingsModelId = button.dataset.settingsModel; renderModelEditor(); }));
      bindSettingsOrder(els.settingsModelList, "model", provider);
      const model = currentSettingsModel();
      if (!model) { els.modelForm.innerHTML = '<div class="provider-empty">点击“新增模型”开始配置。</div>'; return; }
      ensureToolConfig(model, provider);
      const tabs = `<div class="model-tabs"><button class="model-tab ${state.modelEditorTab === "model" ? "is-active" : ""}" data-model-tab="model" type="button">${comfy ? "工作流配置" : "模型配置"}</button><button class="model-tab ${state.modelEditorTab === "tool" ? "is-active" : ""}" data-model-tab="tool" type="button">工具配置</button></div>`;
      els.modelForm.innerHTML = state.modelEditorTab === "tool" ? `${tabs}${renderToolConfiguration(model)}` : `${tabs}${renderModelConfiguration(provider, model)}`;
      els.modelForm.querySelectorAll("[data-model-tab]").forEach((button) => button.addEventListener("click", () => { state.modelEditorTab = button.dataset.modelTab; renderModelEditor(); }));
      bindModelConfiguration(provider, model);
      if (comfy) hooks.comfyui().bindConfiguration(provider, model);
      window.ImageStudioSelect?.refresh(els.modelForm);
    }
    function renderModelConfiguration(provider, model) {
      if (provider.kind === "comfyui") {
        const schedulingField = (key, label, value) => `<label class="field">${label}<input data-model-field="${key}" type="number" min="1" max="16" step="1" value="${escape(value)}" /></label>`;
        return `<h3>${escape(model.name || model.id)}</h3>${modelField("id", "工作流 ID", model.id)}${modelField("name", "显示名称", model.name)}${hooks.comfyui().configuration(model)}${schedulingField("max_concurrent_requests", "工作流最大并发请求数", model.max_concurrent_requests || 8)}${schedulingField("native_batch_size", "工作流单次出图张数", model.native_batch_size || 1)}<p class="field-hint field-wide">按单次出图张数安排执行轮次，每轮仅保留计划张数。超出的结果截断，结果不足不会补跑。</p><section class="schema-preview"><h4>参数默认值</h4>${effectiveModelParameters(model).map(([name, descriptor]) => renderSchemaDefault(name, descriptor)).join("")}</section><details class="schema-raw"><summary>高级：参数 Schema</summary><textarea id="modelParametersSchema" data-model-field="parameters" rows="12" spellcheck="false">${escape(JSON.stringify(model.parameters || {}, null, 2))}</textarea></details><div class="provider-editor-actions"><button class="danger-button" id="removeModelButton" type="button">删除工作流</button></div>`;
      }
      const official = provider.kind === "novelai_official";
      const schemaText = JSON.stringify(model.parameters || modelPreset(provider.kind, model.id), null, 2);
      const discoveredIds = (provider.discovered_models || []).map((item) => item.id); const modelChoices = builtinProviderModels(provider)?.map(item => item.id) || discoveredIds;
      const modelIdField = modelChoices.length ? modelSelectField("id", "模型 ID", model.id, modelChoices, !official) : modelField("id", "模型 ID", model.id);
      const negativeDefaultField = model.supports_negative_prompt ? modelTextAreaField("negative_prompt_default", "默认反向提示词", model.negative_prompt_default || "") : "";
      const testDisabled = !model.supports_text2img;
      const defaults = `<section class="schema-preview"><h4>参数默认值</h4>${effectiveModelParameters(model).map(([name, descriptor]) => renderSchemaDefault(name, descriptor)).join("") || '<span class="field-hint">当前 schema 没有参数。</span>'}</section>`;
      const batchFields = `<div class="field">${schemaParameterLabel("native_batch_size", { label: "单次请求图片上限", description: "单次接口请求最多生成的图片张数；总张数超出时自动分批。" })}<input data-model-field="native_batch_size" aria-label="单次请求图片上限" type="number" min="1" step="1" value="${escape(model.native_batch_size)}"${provider.kind === "nai_direct" ? " disabled" : ""} />${official ? '<span class="field-hint">默认每次请求 1 张；提高此值使用官方多样本生成，额外样本可能消耗 Anlas。</span>' : ""}</div><div class="field">${schemaParameterLabel("max_concurrent_requests", { label: "模型最大并发请求数", description: "该模型在所有任务中共享的最大并发请求数，仍受服务商最大并发限制。取值范围：[1, 16]。" })}<input data-model-field="max_concurrent_requests" aria-label="模型最大并发请求数" type="number" min="1" max="16" step="1" value="${escape(model.max_concurrent_requests)}" />${official ? '<span class="field-hint">官方服务商当前串行处理，实际同时执行 1 个请求。</span>' : ""}</div>`;
      const raw = `<details class="schema-raw"><summary>高级：参数 Schema</summary><textarea id="modelParametersSchema" data-model-field="parameters" rows="14" spellcheck="false">${escape(schemaText)}</textarea><span class="field-hint">每个字段支持 type、label、description、default、request_key、min、max、step、choices、modes、webui_visible、record_in_history、refill_from_history。</span></details>`;
      const capabilityEditable = !official && ["unknown", "manual"].includes(model.capability_source);
      const capabilityHint = model.supports_img2img ? `<div class="field"><label>参考图能力上限</label><input data-model-field="max_reference_images" type="number" min="1" max="8" step="1" value="${configuredReferenceLimit(model.max_reference_images)}"${capabilityEditable ? "" : " disabled"} /><span class="field-hint">${official ? "底图与蒙版分别最多 1 张；V4.5 可组合参考图，总计最多 8 张，V5 最多使用底图与蒙版共 2 张。" : capabilityEditable ? "无法获取时可手动填写，取值范围：1–8 张。" : `来源：${escape(model.capability_source)}，已获取的能力不可在此覆盖。`}</span></div>` : "";
      return `<h3>${escape(model.name || model.id)}</h3>${modelIdField}${modelField("name", "显示名称", model.name)}${batchFields}${modelToggle("supports_text2img", "支持文生图", model.supports_text2img)}${modelToggle("supports_img2img", "支持图生图", model.supports_img2img, provider.kind === "nai_direct")}${modelToggle("supports_negative_prompt", "支持专用反向提示词", model.supports_negative_prompt, provider.kind === "gemini")}${capabilityHint}${negativeDefaultField}${defaults}${raw}<div class="provider-editor-actions"><button class="danger-button" id="removeModelButton" type="button">删除模型</button><button class="quiet-button" id="testModelButton" type="button"${testDisabled ? ' disabled data-tooltip="仅支持图生图的模型需要参考图，暂不能在此测试"' : ""}>测试模型</button></div>`;
    }
    function renderSchemaDefault(name, descriptor) {
      const type = String(descriptor.type || "text").toLowerCase();
      const value = descriptor.default ?? "";
      const label = `<div class="field-label-row">${schemaParameterLabel(name, descriptor)}${library.schemaPolicyButton(name)}</div>`;
      const accessibleLabel = escape(schemaParameterTitle(name, descriptor));
      if ((type === "select" || type === "preset") && Array.isArray(descriptor.choices)) return `<div class="field">${label}<select aria-label="${accessibleLabel} 默认值" data-schema-default="${escape(name)}">${descriptor.choices.map((choice) => { const item = typeof choice === "object" ? choice : { value: choice, label: choice }; return `<option value="${escape(item.value)}" ${String(item.value) === String(value) ? "selected" : ""}>${escape(item.label || item.value)}</option>`; }).join("")}</select></div>`;
      if (type === "boolean" || type === "bool") return `<div class="field">${label}<label class="toggle-control"><input data-schema-default="${escape(name)}" aria-label="${accessibleLabel} 默认值" type="checkbox" ${value ? "checked" : ""} /><span aria-hidden="true"></span></label></div>`;
      if (["json", "object"].includes(type)) return `<div class="field field-wide">${label}<textarea aria-label="${accessibleLabel} 默认值" data-schema-default="${escape(name)}" data-schema-json="true" rows="2" spellcheck="false">${escape(typeof value === "string" ? value : JSON.stringify(value, null, 2))}</textarea></div>`;
      const inputType = ["number", "int", "integer", "float"].includes(type) ? "number" : "text";
      return `<div class="field">${label}<input aria-label="${accessibleLabel} 默认值" data-schema-default="${escape(name)}" type="${inputType}" value="${escape(value)}"${descriptor.min !== undefined ? ` min="${escape(descriptor.min)}"` : ""}${descriptor.max !== undefined ? ` max="${escape(descriptor.max)}"` : ""}${descriptor.step !== undefined ? ` step="${escape(descriptor.step)}"` : ""} /></div>`;
    }
    function toolModelParameters(model) {
      const entries = effectiveModelParameters(model).filter(([name, descriptor]) => name !== "negative_prompt" && (!descriptor.ui_only || String(descriptor.type).toLowerCase() === "preset"));
      if (model.supports_negative_prompt) entries.unshift(["negative_prompt", { type: "textarea", label: "反向提示词", description: "专用反向提示词，只填写不希望出现在画面中的内容；省略时使用此处的默认值。", default: model.negative_prompt_default ?? "" }]);
      return entries;
    }
    function toolParameterDescriptor(model, name) { return toolModelParameters(model).find(([key]) => key === name)?.[1] || {}; }
    function renderToolConfiguration(model) {
      const tool = model.tool;
      const comfy = currentSettingsProvider()?.kind === "comfyui";
      const profileChoices = ["natural_language", "nai_tags", "custom"];
      if (comfy) profileChoices.unshift("");
      const profileField = modelSelectField("tool_prompt_profile", "提示词类型", tool.prompt_profile || (comfy ? "" : "natural_language"), profileChoices, false, { "": "未指定" });
      const configuredLimit = configuredReferenceLimit(model.max_reference_images);
      const refLimit = model.supports_img2img ? `<div class="field"><label>LLM 最大参考图数量</label><input data-tool-field="max_reference_images" type="number" min="1" max="${configuredLimit}" step="1" value="${configuredReferenceLimit(tool.max_reference_images, configuredLimit)}" /><span class="field-hint">不能超过模型能力上限 ${configuredLimit}</span></div>` : "";
      const rows = toolModelParameters(model).map(([name, descriptor]) => {
        const policy = tool.parameters?.[name] || {};
        const label = schemaParameterLabel(name, { ...descriptor, description: policy.description || descriptor.description }, "strong");
        return `<div class="tool-parameter-row">${label}<span>${policy.exposed === false ? "未暴露" : "已暴露"}</span><button class="quiet-button" data-edit-tool-parameter="${escape(name)}" type="button">编辑</button></div>`;
      }).join("");
      return `<h3>${escape(model.name || model.id)}</h3>${modelToggle("tool_enabled", comfy ? "允许 LLM 调用此工作流" : "允许 LLM 调用此模型", tool.enabled !== false)}${modelTextAreaField("tool_selection_description", "什么时候使用", tool.selection_description || "")}${profileField}${modelTextAreaField("tool_prompt_instructions", "提示词编写要求", tool.prompt_instructions || "")}${refLimit}<div class="tool-parameter-list"><span class="field-hint">LLM 可用参数</span>${rows || '<span class="field-hint">当前模型没有可暴露参数。</span>'}</div>`;
    }
    function bindModelConfiguration(provider, model) {
      els.modelForm.querySelectorAll("[data-model-field]").forEach((input) => { input.addEventListener("input", () => updateModelField(input)); input.addEventListener("change", () => updateModelField(input, true)); });
      els.modelForm.querySelectorAll("[data-schema-default]").forEach((input) => input.addEventListener("change", () => {
        const descriptor = model.parameters[input.dataset.schemaDefault];
        if (input.dataset.schemaJson) { try { descriptor.default = JSON.parse(input.value); input.setCustomValidity(""); } catch { input.setCustomValidity("默认值必须是合法 JSON"); return; } }
        else descriptor.default = input.type === "checkbox" ? input.checked : input.type === "number" ? provider.kind === "comfyui" ? hooks.comfyui().numericValue(input.value) : Number(input.value) : input.value;
        const raw = $("modelParametersSchema"); if (raw) raw.value = JSON.stringify(model.parameters, null, 2);
      }));
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
    function modelSelectField(key, label, value, choices, editable = false, labels = {}) { const values = choices.includes(value) ? choices : [value, ...choices]; return `<div class="field"><label>${label}</label><select data-model-field="${key}">${values.map((item) => `<option value="${escape(item)}" ${item === value ? "selected" : ""}>${escape(labels[item] ?? item)}</option>`).join("")}${editable ? '<option value="__manual__">手动输入…</option>' : ""}</select></div>`; }
    function modelToggle(key, label, value, disabled = false) { return `<div class="toggle-row"><label>${label}</label><label class="toggle-control"><input data-model-field="${key}" type="checkbox" ${value ? "checked" : ""}${disabled ? " disabled" : ""} /><span aria-hidden="true"></span></label></div>`; }
    function reconcileOfficialParameters(current, preset) {
      const keyOf = ([name, descriptor]) => descriptor.request_key || name;
      const knownKeys = new Set(novelaiModels.flatMap(model => Object.entries(model.parameters || {}).map(keyOf)));
      const previous = new Map(Object.entries(current || {}).map(entry => [keyOf(entry), entry]));
      const result = Object.fromEntries(Object.entries(current || {}).filter(entry => !knownKeys.has(keyOf(entry))));
      for (const [name, descriptor] of Object.entries(preset.parameters || {})) {
        const key = descriptor.request_key || name, existing = previous.get(key);
        const merged = { ...(existing?.[1] || {}), ...structuredClone(descriptor) };
        for (const field of ["default", "webui_visible", "record_in_history", "refill_from_history"]) if (existing && Object.prototype.hasOwnProperty.call(existing[1], field)) merged[field] = structuredClone(existing[1][field]);
        if (["reference_mode", "noise_schedule", "sampler", "quality_preset"].includes(key) && descriptor.choices && !descriptor.choices.some(choice => (typeof choice === "object" ? choice.value : choice) === merged.default)) merged.default = descriptor.default;
        result[existing?.[0] || name] = merged;
      }
      return result;
    }
    function updateModelField(input, commit = false) {
      const model = currentSettingsModel(); if (!model) return;
      const key = input.dataset.modelField;
      if (key === "parameters") { try { model.parameters = input.value.trim() ? JSON.parse(input.value) : {}; input.setCustomValidity(""); } catch { input.setCustomValidity("参数 schema 必须是合法 JSON"); } return; }
      if (key.startsWith("tool_")) { const toolKey = key.slice(5); model.tool[toolKey] = input.type === "checkbox" ? input.checked : input.value; if (toolKey === "enabled") refreshSettingsDefaultModels(); return; }
      if (key === "id" && input.value === "__manual__") {
        if (commit) void editManualModelId(input, model);
        return;
      }
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
        if (currentSettingsProvider()?.kind === "novelai_official") {
          const preset = novelaiModels.find(item => item.id === input.value);
          if (preset) { model.novelai_capabilities = structuredClone(preset.novelai_capabilities); model.max_reference_images = preset.max_reference_images; model.tool.max_reference_images = Math.min(model.tool.max_reference_images, model.max_reference_images); model.parameters = reconcileOfficialParameters(model.parameters, preset); }
        }
        state.selectedSettingsModelId = input.value;
        updateSettingsRowId(els.settingsModelList, "model", input.value);
        const activeRow = els.settingsModelList.querySelector(".provider-row.is-active");
        if (activeRow && !model.name) activeRow.querySelector("strong").textContent = input.value;
        if (currentSettingsProvider()?.kind === "novelai_official" && commit) renderModelEditor();
      }
      refreshSettingsDefaultModels();
    }

    async function editManualModelId(input, model) {
      input.value = model.id;
      window.ImageStudioSelect?.refresh(input);
      const value = await library.openModal("输入模型 ID", `<label class="field">模型 ID<input id="manualModelId" value="${escape(model.id)}" autocomplete="off" /></label>`, [
        { label: "取消", action: () => false },
        { label: "确认", primary: true, action: () => {
          const id = $("manualModelId").value.trim();
          if (!id || id === "__manual__") throw new Error("请填写有效的模型 ID。");
          return id;
        } },
      ], { focus: "manualModelId" });
      if (!value || currentSettingsModel() !== model || !input.isConnected) return;
      if (!Array.from(input.options).some(option => option.value === value)) input.add(new Option(value, value));
      input.value = value;
      updateModelField(input, true);
      window.ImageStudioSelect?.refresh(input);
      updateSettingsDirty();
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
      const official = provider.kind === "novelai_official";
      model.max_reference_images = official ? Number(model.novelai_capabilities?.max_reference_images || novelaiModels.find(item => item.id === model.id)?.max_reference_images || (model.id.startsWith("nai-diffusion-5-") ? 2 : 8)) : configuredReferenceLimit(model.max_reference_images);
      if (nai) model.supports_img2img = false;
      const defaults = { enabled: true, selection_description: nai ? "仅在用户明确要求 NAI 或 NovelAI 风格标签生图时使用。" : "适合一般自然语言生图需求。", prompt_profile: nai ? "nai_tags" : "natural_language", prompt_instructions: nai ? "使用英文逗号分隔标签。必须完整描述主体数量、全身或半身范围、姿态、镜头距离、视角、背景、光照和画面边界，避免残图；不得改变用户明确指定的主体、数量、动作和服装。" : "使用清晰、连贯的自然语言描述，不要使用英文逗号分隔的 NAI tag 串。", negative_prompt_exposed: !!model.supports_negative_prompt, max_reference_images: model.max_reference_images, parameters: {} };
      if (official) Object.assign(defaults, { selection_description: "适合 NovelAI 插画生成，支持英文标签或自然语言提示词。", prompt_profile: "custom", prompt_instructions: "支持英文逗号分隔标签或清晰的自然语言描述，可结合两者表达。完整描述主体、构图、动作、环境与光照，保留用户明确指定的内容。" });
      if (provider.kind === "comfyui") Object.assign(defaults, { selection_description: "", prompt_profile: "", prompt_instructions: "" });
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
      $("parameterDialogTitle").innerHTML = `编辑工具参数：${schemaParameterLabel(name, { ...descriptor, description: policy.description || descriptor.description }, "span")}`;
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
      window.ImageStudioDialogMotion.show(els.parameterDialog); syncPageScrollLock();
      window.ImageStudioSelect?.refresh(els.parameterDialog);
    }
    function closeToolParameterDialog() {
      state.editingToolParameter = ""; state.editingToolDefaultChoices = [];
      window.ImageStudioSelect?.close();
      window.ImageStudioDialogMotion.hide(els.parameterDialog, syncPageScrollLock);
      syncPageScrollLock();
    }
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
        policy.default_override = ["number", "int", "integer", "float"].includes(String(descriptor.type).toLowerCase()) ? currentSettingsProvider()?.kind === "comfyui" ? hooks.comfyui().numericValue(els.toolParameterDefault.value) : Number(els.toolParameterDefault.value) : els.toolParameterDefault.value;
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
      state.settings.webui.providers.push({ id, name: "新服务商", enabled: true, kind: "openai_images", ...providerDefaults("openai_images"), api_key: "", proxy: "", custom_headers: "", timeout_seconds: 180, discovered_models: [], models: [] });
      state.selectedSettingsProviderId = id; state.selectedSettingsModelId = ""; renderSettingsProviders(); showNotice("已新增生图服务商，请填写连接配置并添加模型。", "success");
    }
    async function addModel() {
      if (!state.settings && !await loadSettings()) return;
      const provider = currentSettingsProvider();
      if (!provider) { showNotice("请先选择一个服务商，再新增模型。", "error"); return; }
      provider.models = Array.isArray(provider.models) ? provider.models : [];
      if (provider.kind === "comfyui") {
        const requestedId = els.newModelChoice.value.trim() || `workflow_${Date.now().toString(36)}`;
        if (provider.models.some(item => item.id === requestedId)) { showNotice("该服务商中已经存在相同工作流 ID。", "error"); return; }
        const result = await hooks.comfyui().edit(provider, { id: requestedId, name: requestedId });
        if (!result) return;
        provider.models.push(result.model); state.selectedSettingsModelId = requestedId;
        renderModelEditor(); updateSettingsDirty(); showNotice("工作流已加入草稿，请保存全部设置。", "success"); return;
      }
      const requestedId = els.newModelChoice.value.trim();
      if (!requestedId) { showNotice("请选择或输入模型 ID。", "error"); els.newModelChoice.focus(); return; }
      if (provider.models.some((item) => item.id === requestedId)) { showNotice("该服务商中已经存在相同模型 ID。", "error"); return; }
      const official = provider.kind === "novelai_official";
      const discovered = builtinProviderModels(provider) ? null : (provider.discovered_models || []).find((item) => item.id === requestedId);
      const naiChoice = builtinProviderModels(provider)?.find((item) => item.id === requestedId);
      if (official && !naiChoice) { showNotice("请选择内置的 NovelAI 官方模型。", "error"); return; }
      if (official && !naiChoice.parameters) { showNotice("未能读取官方模型预设，请刷新页面后重试。", "error"); return; }
      const chosen = discovered || (naiChoice ? { ...naiChoice } : null);
      const capabilityKnown = !!chosen?.capability_source && chosen.capability_source !== "unknown";
      const maxRefs = capabilityKnown ? configuredReferenceLimit(chosen.max_reference_images) : 1;
      provider.models.push({ id: requestedId, name: chosen?.name || requestedId, native_batch_size: provider.kind === "nai_direct" ? 1 : Number(chosen?.native_batch_size) || 1, native_batch_size_source: provider.kind === "nai_direct" ? "fixed" : chosen?.native_batch_size_source || "default", max_concurrent_requests: 8, supports_text2img: chosen ? !!chosen.supports_text2img : true, supports_img2img: provider.kind !== "nai_direct" && capabilityKnown ? !!chosen.supports_img2img : false, supports_negative_prompt: chosen ? !!chosen.supports_negative_prompt : provider.kind === "nai_direct", negative_prompt_default: provider.kind === "nai_direct" ? NAI_DEFAULT_NEGATIVE : "", max_reference_images: maxRefs, capability_source: chosen?.capability_source || "manual", ...(chosen?.novelai_capabilities ? { novelai_capabilities: structuredClone(chosen.novelai_capabilities) } : {}), parameters: modelPreset(provider.kind, requestedId), tool: { enabled: true, max_reference_images: maxRefs } });
      state.selectedSettingsModelId = requestedId; renderModelEditor(); showNotice("已新增模型，请填写能力和参数 schema。", "success");
    }
    async function saveSettings() {
      if (settingsSaving) return;
      if (!await loadSettings()) return;
      await window.ImageStudioAppearance.ready;
      if (settingsSaving) return;
      const invalid = $("settingsView").querySelector('input:invalid, textarea:invalid, select:invalid, [aria-invalid="true"]');
      if (invalid) { invalid.reportValidity?.(); setError(els.settingsError, "请先修正无效的设置项。"); return; }
      settingsSaving = true; setError(els.settingsError, "正在保存设置…"); els.saveSettingsButton.disabled = true; library.setCommandLabel("saveSettingsButton", "保存中…");
      const draft = settingsDraft(); const submitted = settingsFingerprint(draft); const webui = draft.studio;
      const appearance = window.ImageStudioAppearance;
      const appearanceSnapshot = appearance.get(), saveAppearance = appearance.isDirty();
      const sortSnapshot = gallerySortDraft, saveSort = sortSnapshot !== hooks.getGallerySort();
      let savedAny = false;
      try {
        let warnings = [];
        if (submitted !== settingsBaseline) {
          const saved = await apiPost("settings/save", { settings_revision: webui.revision ?? webui.ui?.settings_revision, ...draft });
          savedAny = true;
          warnings = Array.isArray(saved?.warnings) ? saved.warnings.filter(Boolean) : [];
          const confirmed = { base: structuredClone(draft.base), webui: structuredClone(draft.studio), validation_errors: [] };
          const revision = Number(saved?.settings_revision ?? Number(webui.revision ?? webui.ui?.settings_revision ?? 0) + 1);
          confirmed.webui.revision = revision;
          confirmed.webui.ui = { ...(confirmed.webui.ui || {}), settings_revision: revision };
          // The write is already committed. Keep its acknowledged snapshot even
          // if either follow-up request fails, without replacing newer edits.
          adoptConfirmedSettings(confirmed, draft.studio.external_sources);
          settingsReadPending = true;
          settingsBootstrapPending = true;
          try { await rereadConfirmedSettings(); }
          catch (error) { warnings.push(`设置已保存，但重新读取失败：${errorMessage(error, "请稍后重试")}`); }
        }
        if (settingsBootstrapPending) {
          try { await bootstrap(); settingsBootstrapPending = false; }
          catch (error) { warnings.push(`设置已保存，但生图面板刷新失败：${errorMessage(error, "请稍后重试")}`); }
        }
        if (saveAppearance) { await appearance.save(appearanceSnapshot); savedAny = true; }
        if (saveSort) {
          await window.ImageStudioGalleryPreferences.setSort(sortSnapshot);
          savedAny = true; hooks.onGallerySortSaved(sortSnapshot);
        }
        setError(els.settingsError, warnings.join("；")); showNotice(warnings.length ? `设置已保存。${warnings.join("；")}` : "设置已保存并生效。", warnings.length ? "info" : "success");
        await loadStorageHealth();
        await hooks.externalSources().refresh();
      } catch (error) {
        const message = `${savedAny ? "部分设置已保存，其余更改仍未保存：" : ""}${errorMessage(error, "设置保存失败")}`; setError(els.settingsError, message); showNotice(message, "error");
      } finally { settingsSaving = false; els.saveSettingsButton.disabled = false; library.setCommandLabel("saveSettingsButton", "保存全部设置"); updateSettingsDirty(); }
    }

    function adoptConfirmedSettings(payload, submittedSources) {
      const confirmed = structuredClone(payload);
      normalizeSettingsModelDefaults(confirmed);
      savedSettingsPayload = confirmed;
      settingsBaseline = settingsFingerprint({ base: confirmed.base, studio: confirmed.webui });
      if (state.settings) {
        state.settings.webui.revision = confirmed.webui.revision;
        state.settings.webui.ui = { ...(state.settings.webui.ui || {}), settings_revision: confirmed.webui.revision };
        state.settings.webui.external_sources = structuredClone(confirmed.webui.external_sources || {});
        hooks.externalSources().settingsLoaded(true, submittedSources || confirmed.webui.external_sources || {});
      }
      updateSettingsDirty();
    }

    function rereadConfirmedSettings() {
      if (settingsReadPromise) return settingsReadPromise;
      const previous = structuredClone(savedSettingsPayload);
      const baseline = settingsBaseline;
      settingsReadPromise = (async () => {
        const server = await apiGet("settings/get");
        if (!server?.base || !server?.webui || !Array.isArray(server.webui.providers)) throw new Error("设置接口返回的数据格式无效");
        if (savedSettingsPayload?.webui.revision !== previous?.webui.revision) return;
        const revision = Number(server.webui.revision ?? server.webui.ui?.settings_revision);
        if (!Number.isFinite(revision) || revision < Number(previous.webui.revision)) throw new Error("尚未读取到最新的已保存设置");
        normalizeSettingsModelDefaults(server);
        if (settingsFingerprint(settingsDraft()) === baseline) {
          if (!await loadSettings(true, server)) throw new Error("无法更新已保存的设置");
        } else adoptConfirmedSettings(server, previous.webui.external_sources);
        settingsReadPending = false;
      })().finally(() => { settingsReadPromise = null; });
      return settingsReadPromise;
    }

    function settingsDraft() {
      if (!state.settings) return null;
      const webui = JSON.parse(JSON.stringify(state.settings.webui));
      webui.history = { ...(webui.history || {}), enabled: els.historyEnabled.checked, retain_reference_images: els.retainReferences.checked, record_invocation_identity: els.recordInvocationIdentity.checked, max_records: Number(els.historyRecords.value), max_megabytes: Number(els.historyMegabytes.value) };
      webui.llm_policy = { ...(webui.llm_policy || {}), image_return_mode: els.agentImageReturnMode.value };
      webui.asset_policy = { preview_max_edge: Number(els.agentPreviewMaxEdge.value), preview_quality: Number(els.agentPreviewQuality.value) };
      webui.generation_defaults = { page: { text2img_model_ref: els.settingPageDefaultTextModel.value, img2img_model_ref: els.settingPageDefaultImageModel.value }, tool: { text2img_model_ref: els.settingToolDefaultTextModel.value, img2img_model_ref: els.settingToolDefaultImageModel.value } };
      hooks.externalSources().settingsDraft(webui);
      return { base: { enable_llm_tool: els.settingTool.checked }, studio: webui };
    }

    function normalizeSettingsModelDefaults(payload) {
      (payload.webui?.providers || []).forEach((provider) => (provider.models || []).forEach((model) => ensureToolConfig(model, provider)));
    }

    async function prepareComfyWorkflowSettings(providerId) {
      if (!await loadSettings()) throw new Error("设置读取失败，尚未添加工作流，请重试。");
      const provider = state.settings.webui.providers.find(item => item.id === providerId && item.kind === "comfyui");
      if (!provider) throw new Error("设置草稿中已没有此 ComfyUI 服务商，请重新选择。");
      state.selectedSettingsProviderId = providerId; state.selectedSettingsModelId = ""; state.modelEditorTab = "model";
      closeDetail(); switchView("settings"); renderSettingsProviders();
      return provider;
    }

    function addComfyWorkflowDraft(result) {
      const provider = state.settings?.webui?.providers?.find(item => item.id === result.provider.id && item.kind === "comfyui");
      if (!provider) throw new Error("目标 ComfyUI 服务商已从设置草稿中删除，请重试。");
      provider.models = Array.isArray(provider.models) ? provider.models : [];
      if (provider.models.some(item => item.id === result.model.id)) throw new Error("工作流 ID 已存在，请使用其他 ID。");
      const model = structuredClone(result.model); ensureToolConfig(model, provider); provider.models.push(model);
      state.selectedSettingsProviderId = provider.id; state.selectedSettingsModelId = model.id; state.modelEditorTab = "model";
      renderSettingsProviders(); updateSettingsDirty();
      showNotice("工作流已加入设置草稿，点击保存全部设置后生效。", "success");
    }

    function settingsFingerprint(value) {
      const copy = value ? JSON.parse(JSON.stringify(value)) : value;
      if (copy?.studio) { delete copy.studio.revision; if (copy.studio.ui) delete copy.studio.ui.settings_revision; }
      const normalize = (item) => Array.isArray(item) ? item.map(normalize) : item && typeof item === "object" ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, normalize(item[key])])) : item;
      return JSON.stringify(normalize(copy));
    }

    function settingsDirty() {
      return !!window.ImageStudioAppearance?.isDirty() || gallerySortDraft !== hooks.getGallerySort()
        || !!$("settingsView").querySelector('input:invalid, textarea:invalid, select:invalid, [aria-invalid="true"]')
        || (!!state.settings && !!settingsBaseline && settingsFingerprint(settingsDraft()) !== settingsBaseline);
    }

    function updateSettingsDirty() {
      const dirty = settingsDirty();
      els.saveSettingsButton.classList.toggle("is-dirty", dirty); $("settingsDirtyStatus").textContent = dirty ? "有未保存的更改" : state.settings ? "已保存" : "";
      $("settingsDirtyStatus").classList.toggle("is-saved", !dirty && !!state.settings);
      renderStorageQuotas();
    }

    function bind() {
      if (bound) return;
      bound = true;
      const bindGallerySort = () => {
        const select = $("gallerySort");
        if (!select || select.dataset.bound) return;
        select.dataset.bound = "true"; select.value = gallerySortDraft;
        select.addEventListener("change", () => {
          gallerySortDraft = select.value === "latest_content" ? "latest_content" : "created";
          updateSettingsDirty();
        });
        window.ImageStudioSelect?.refresh(select);
      };
      bindGallerySort();
      window.addEventListener("image-studio-display-settings-ready", bindGallerySort);
      window.addEventListener("image-studio-appearance-change", updateSettingsDirty);
      window.addEventListener("beforeunload", event => {
        if (hooks.getView() !== "settings" || !settingsDirty()) return;
        event.preventDefault(); event.returnValue = "";
      });
      ["input", "change", "click"].forEach((name) => $("settingsView").addEventListener(name, () => window.setTimeout(updateSettingsDirty, 0)));
      $("parameterDialogApply").addEventListener("click", () => window.setTimeout(updateSettingsDirty, 0));
      document.querySelectorAll("[data-default-scope]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-default-scope]").forEach((item) => item.classList.toggle("is-active", item === button)); document.querySelectorAll("[data-default-panel]").forEach((panel) => panel.classList.toggle("is-hidden", panel.dataset.defaultPanel !== button.dataset.defaultScope)); }));
      els.agentImageReturnMode.addEventListener("change", syncAgentImageSettings);
      els.runMaintenanceButton.addEventListener("click", () => void runStorageMaintenance(false)); els.runDeepMaintenanceButton.addEventListener("click", () => void runStorageMaintenance(true));
      $("parameterDialogCancel").addEventListener("click", closeToolParameterDialog); $("parameterDialogApply").addEventListener("click", applyToolParameterDialog); els.addProviderButton.addEventListener("click", () => void addProvider()); els.addModelButton.addEventListener("click", () => void addModel()); els.saveSettingsButton.addEventListener("click", () => void saveSettings());
    }

    return { bind, loadSettings, loadStorageHealth, updateSettingsDirty, currentSettingsModel, renderModelEditor, prepareComfyWorkflowSettings, addComfyWorkflowDraft, closeToolParameterDialog, leaveSettings, settingsDirty,
      isSaving: () => settingsSaving,
      getSettings: () => state.settings,
      setNovelAIModels: models => { if (Array.isArray(models)) novelaiModels = models; },
      resetGallerySort: () => { gallerySortDraft = hooks.getGallerySort(); },
    };
  };
})();
