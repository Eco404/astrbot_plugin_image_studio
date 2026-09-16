(function () {
  "use strict";

  window.ImageStudioComfyUI = function (hooks) {
    const { state, escape, apiGet, apiPost, bridge, showNotice } = hooks;
    const $ = id => document.getElementById(id);
    const sources = { parameter: "普通参数", prompt: "主提示词", negative_prompt: "反向提示词", width: "宽度", height: "高度", seed: "种子", reference: "参考图 / 蒙版" };
    const executionPolicy = "fixed_outputs_v1";
    const reservedCountKeys = new Set(["count", "n"]);
    const terminal = new Set(["completed", "succeeded", "success", "partial", "failed", "cancelled", "canceled", "interrupted", "submission_unknown", "unknown"]);
    const successful = new Set(["completed", "succeeded", "success"]);
    const problematic = new Set(["partial", "failed", "interrupted", "submission_unknown", "unknown"]);
    const jobs = new Map();
    let timer = 0, polling = false;
    const active = model => model?.provider_kind === "comfyui";
    const bindingList = definition => Object.values(definition?.bindings || {});
    const promptRequired = model => !active(model) || (model.prompt_required ?? model.comfyui_capabilities?.prompt_required ?? bindingList(model.comfyui).some(binding => binding.source === "prompt"));
    function totalParameters(parameters = {}) {
      const current = parameters?.count && typeof parameters.count === "object" ? parameters.count : {};
      return { ...parameters, count: { type: "integer", label: "生图张数", description: "本次目标总张数，按工作流单次出图张数安排执行轮次。每轮只保留计划张数，超出截断，不足不补。", default: 1, min: 1, max: 16, step: 1, ...current, request_key: "count", refill_from_history: false } };
    }
    const option = (value, title, selected, disabled = false) => `<option value="${escape(value)}"${selected ? " selected" : ""}${disabled ? " disabled" : ""}>${escape(title)}</option>`;
    const nodeTitle = (id, node) => `#${id} · ${node?._meta?.title || node?.class_type || "节点"}`;
    const errorText = error => hooks.errorMessage(error, "工作流操作失败");
    const compatible = report => !!report && report.compatible !== false && report.valid !== false && !(report.issues || []).some(item => item.severity === "error");
    function seedNoticeMarkup(warnings) {
      if (!warnings?.length) return "";
      return `<div class="comfy-seed-notice" role="status"><div><strong>种子设置提示</strong><ul>${warnings.map(item => `<li>${escape(`节点 #${item.node_id} · ${item.input_name} = ${item.value}`)}<br>${escape(item.message || "如需随机种子，请将对应输入来源设为“种子”，再将其默认值改为 -1。")}</li>`).join("")}</ul></div><button type="button" class="studio-icon-button" data-dismiss-seed-warning aria-label="关闭种子提示"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg></button></div>`;
    }
    function temporaryModel(model) {
      const { provider_id, provider_name, provider_kind, model_ref, temporary, seed_warnings, seed_warning_dismissed, ...config } = model;
      return structuredClone(config);
    }
    function applyTemporary(result, references = [], warnings = []) {
      const model = result.model;
      const definition = displayDefinition(model.comfyui);
      const originalText = source => {
        const binding = bindingList(definition).find(binding => binding.source === source), target = binding?.targets?.[0];
        return target ? String(definition.api_graph[target.node_id]?.inputs?.[target.input_name] ?? "") : "";
      };
      hooks.applyDraft({ mode: model.supports_img2img ? "img2img" : "text2img", provider_id: result.provider.id, model_ref: result.model_ref || `${result.provider.id}:${model.id}`, model: model.id, temporary_model: model, comfyui: model.comfyui, prompt: originalText("prompt"), negative_prompt: originalText("negative_prompt"), parameters: {}, count: 1, references, seed_warnings: result.seed_warnings || [], notice: warnings.join("；") });
    }
    function renderWorkspace(model) {
      let host = $("comfyTemporaryInfo");
      if (!host) { host = document.createElement("section"); host.id = "comfyTemporaryInfo"; host.className = "comfy-page-draft"; $("modelParameters").before(host); }
      host.hidden = !model?.temporary;
      if (!model?.temporary) { host.replaceChildren(); return; }
      const signature = JSON.stringify(model.seed_warnings || []);
      host.innerHTML = `<div class="comfy-draft-heading"><p>临时工作流仅在当前页面保留。切换后可重新选择，刷新页面后清除；已提交的任务仍可在队列中查看。</p><button type="button" class="quiet-button" id="comfyEditTemporary">编辑工作流</button></div>${model.seed_warning_dismissed === signature ? "" : seedNoticeMarkup(model.seed_warnings)}`;
      host.querySelector("[data-dismiss-seed-warning]")?.addEventListener("click", event => { if (state.comfyuiTemporaryModel?.model_ref === model.model_ref) state.comfyuiTemporaryModel.seed_warning_dismissed = signature; event.currentTarget.closest(".comfy-seed-notice").remove(); });
      $("comfyEditTemporary").addEventListener("click", async event => {
        const button = event.currentTarget; button.disabled = true;
        try {
          const provider = state.providers.find(item => item.id === model.provider_id && item.kind === "comfyui" && item.enabled !== false);
          if (!provider) throw new Error("目标 ComfyUI 服务商已停用或删除，请重新选择。");
          const result = await edit(provider, temporaryModel(model), { comfyui: model.comfyui, parameters: model.parameters, historical_snapshot: true, seed_warnings: model.seed_warnings }, { temporary: true });
          if (result) applyTemporary(result, state.references);
        } catch (error) { showNotice(errorText(error), "error"); }
        finally { button.disabled = false; }
      });
    }
    // The canonical string remains the execution source. The preview converts
    // only unsafe integer tokens to decimal text so editing cannot round seeds.
    function displayDefinition(value) {
      const definition = structuredClone(value);
      if (definition.api_graph_json) {
        const text = definition.api_graph_json.replace(/"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, token => token[0] !== '"' && /^-?\d+$/.test(token) && !Number.isSafeInteger(Number(token)) ? JSON.stringify(token) : token);
        definition.api_graph = JSON.parse(text);
        for (const item of definition.input_overrides || []) if (definition.api_graph[item.node_id]?.inputs) definition.api_graph[item.node_id].inputs[item.input_name] = item.value;
      }
      return definition;
    }
    function numericValue(value) { return /^-?\d+$/.test(value) && !Number.isSafeInteger(Number(value)) ? value : Number(value); }

    function configuration(model) {
      const definition = model.comfyui || {};
      return `<section class="comfy-workflow-summary field-wide"><div class="field-label-row"><strong>工作流</strong><button type="button" id="comfyEditWorkflow" class="quiet-button">${Object.keys(definition.api_graph || {}).length ? "编辑工作流" : "导入工作流"}</button></div><p class="field-hint">${Object.keys(definition.api_graph || {}).length} 个节点 · ${Object.keys(definition.bindings || {}).length} 个输入绑定 · ${(definition.outputs || []).length} 个结果节点</p><p class="field-hint">本次总张数用于安排执行轮次，节点中的批次参数仍由工作流或对应的普通输入决定。</p><div class="comfy-check-actions"><button type="button" id="comfyCheckWorkflow" class="quiet-button">检查兼容性</button><span id="comfyCheckStatus" role="status"></span></div></section>`;
    }

    function issuesMarkup(report) {
      const issues = [...(report?.issues || []), ...(report?.warnings || []).map(item => typeof item === "string" ? { severity: "warning", message: item } : item)];
      const valid = report?.compatible ?? report?.valid ?? !issues.some(item => item.severity === "error");
      return `<p class="comfy-check-result ${valid ? "is-success" : "is-error"}">${valid ? "已完成检查" : "存在未满足的依赖"}</p>${issues.length ? `<ul class="comfy-issues">${issues.map(issue => `<li>${escape([issue.node_id ? `节点 #${issue.node_id}` : "", issue.input_name || issue.input || "", issue.message || issue.detail || String(issue)].filter(Boolean).join(" · "))}</li>`).join("")}</ul>` : '<p class="field-hint">未发现已知缺失项。动态依赖仍以 ComfyUI 执行校验为准。</p>'}`;
    }

    function bindConfiguration(provider, model) {
      $("comfyEditWorkflow")?.addEventListener("click", async event => {
        const button = event.currentTarget;
        if (button.disabled) return;
        button.disabled = true;
        try {
          const result = await edit(provider, model);
          if (!result || hooks.currentSettingsModel() !== model) return;
          Object.assign(model, result.model);
          hooks.renderModelEditor(); hooks.updateSettingsDirty();
          showNotice("工作流已加入设置草稿，保存全部设置后生效。", "success");
        } catch (error) { showNotice(errorText(error), "error"); }
        finally { button.disabled = false; }
      });
      $("comfyCheckWorkflow")?.addEventListener("click", async event => {
        const button = event.currentTarget, status = $("comfyCheckStatus"); button.disabled = true; status.textContent = "检查中…";
        try {
          const report = await apiPost("comfy/inspect", { provider, comfyui: model.comfyui || {} });
          status.textContent = "检查完成";
          await hooks.openModal("工作流兼容性", issuesMarkup(report), [{ label: "关闭", action: () => true }]);
        } catch (error) { status.textContent = errorText(error); }
        finally { button.disabled = false; }
      });
    }

    async function edit(provider, model = {}, imported = null, options = {}) {
      const original = structuredClone(model);
      if (!imported && original.comfyui?.execution_policy !== executionPolicy && (original.comfyui?.api_graph_json || Object.keys(original.comfyui?.api_graph || {}).length)) {
        // Share the backend migration for legacy node-count bindings and tool
        // aliases. Canceling this editor still leaves the settings untouched.
        const migrated = await apiPost("comfy/import", { comfyui: original.comfyui, parameters: original.parameters || {}, tool: original.tool || {} });
        if (!options.temporary && hooks.currentSettingsModel() !== model) return false;
        original.comfyui = migrated.comfyui;
        original.parameters = migrated.parameters;
        original.tool = migrated.tool || original.tool;
      }
      let definition = displayDefinition(imported?.comfyui || original.comfyui || { api_graph: {}, bindings: {}, outputs: [], execution_policy: executionPolicy });
      let parameters = totalParameters(structuredClone(imported?.parameters || original.parameters || {}));
      let info = {}, report = null, busy = false, revision = 0, alive = true, dragEvents = null;
      let seedWarnings = imported?.seed_warnings || [], dismissedSeedSignature = "", seedTimer = 0, seedRevision = 0;
      const hasWorkflow = () => Object.keys(definition.api_graph || {}).length > 0;
      const syncEditorState = () => {
        $("comfyWorkflowEditing").hidden = !hasWorkflow();
        $("comfyApplyWorkflow").disabled = !hasWorkflow() || busy;
        $("comfyEditor").inert = busy;
      };
      const suggestedRows = (bindings, descriptors = {}) => Object.entries(bindings || {}).map(([key, binding]) => ({ key, label: descriptors[key]?.label || binding.label || key, ...structuredClone(binding) }));
      const freshImport = imported && !imported.historical_snapshot;
      let rows = freshImport ? [] : suggestedRows(definition.bindings, parameters);
      let originalBindingKeys = new Set(Object.keys(definition.bindings || {}));
      let candidates = suggestedRows(imported?.suggestions), identifying = false, identificationRevision = 0;
      if (freshImport) { definition.bindings = {}; delete definition.parameters_schema; parameters = totalParameters(); }
      if (options.temporary) parameters.count = { ...parameters.count, default: 1 };
      if (imported && !definition.outputs?.length) definition.outputs = imported.outputs || [];
      const selectedProvider = () => provider;
      const inputChoices = () => Object.entries(definition.api_graph || {}).flatMap(([id, node]) => Object.entries(node.inputs || {}).filter(([, value]) => !Array.isArray(value) && (value === null || typeof value !== "object")).map(([name, value]) => ({ id, name, value, title: `${nodeTitle(id, node)} → ${name}` })));
      const selectedTargets = row => new Set((row.targets || []).map(target => JSON.stringify([String(target.node_id), target.input_name])));
      const targetOwner = (value, except = -1) => rows.findIndex((row, index) => index !== except && selectedTargets(row).has(value));
      const refreshAvailability = () => {
        const choices = inputChoices();
        $("comfyBindings").querySelectorAll("[data-comfy-binding]").forEach(element => {
          const index = Number(element.dataset.comfyBinding);
          for (const item of element.querySelector("[data-binding-targets]").options) {
            const owner = targetOwner(item.value, index);
            item.disabled = owner >= 0 && !item.selected;
            const title = choices.find(choice => JSON.stringify([choice.id, choice.name]) === item.value)?.title || item.textContent;
            item.textContent = title + (owner >= 0 ? `（已由 ${rows[owner].label || rows[owner].key} 使用）` : "");
          }
        });
        $("comfyAddBinding").innerHTML = '<option value="" selected hidden>添加输入</option>' + option("custom", "自定义", false) + candidates.map((row, index) => {
          const taken = Array.from(selectedTargets(row)).some(value => targetOwner(value) >= 0);
          const title = (row.targets || []).map(target => `${nodeTitle(target.node_id, definition.api_graph[target.node_id])} → ${target.input_name}`).join("、");
          return option(String(index), title + (taken ? "（已添加）" : ""), false, taken);
        }).join("") + (identifying ? option("loading", "正在识别输入…", false, true) : "");
        window.ImageStudioSelect?.refresh($("comfyEditor"));
      };
      const identifyInputs = async () => {
        const current = ++identificationRevision;
        if (!Object.keys(definition.api_graph || {}).length) { candidates = []; identifying = false; refreshAvailability(); return; }
        identifying = true; candidates = []; refreshAvailability();
        try {
          const comfyui = snapshot(), warningRevision = ++seedRevision;
          const payload = await apiPost("comfy/import", { comfyui, parameters: parameterSchema(comfyui) });
          if (alive && current === identificationRevision) {
            candidates = suggestedRows(payload.suggestions);
            if (warningRevision === seedRevision) { seedWarnings = payload.seed_warnings || []; renderSeedWarnings(); }
          }
        } catch (error) { if (alive && current === identificationRevision) showNotice(`识别输入失败，可使用自定义输入：${errorText(error)}`, "error"); }
        finally { if (alive && current === identificationRevision) { identifying = false; refreshAvailability(); } }
      };
      const preserveRows = () => {
        $("comfyBindings")?.querySelectorAll("[data-comfy-binding]").forEach(element => {
          const row = rows[Number(element.dataset.comfyBinding)];
          element.querySelectorAll("[data-binding-field]").forEach(input => {
            const key = input.dataset.bindingField;
            row[key] = key === "reference_index" ? Math.max(0, Number(input.value) - 1) : input.value;
          });
          row.targets = Array.from(element.querySelector("[data-binding-targets]").selectedOptions).map(item => { const [node_id, input_name] = JSON.parse(item.value); return { node_id, input_name }; });
          if (["image", "mask"].includes(row.type)) row.source = "reference";
        });
      };
      const snapshot = () => {
        preserveRows();
        const bindings = {}, occupied = new Map();
        for (const row of rows) {
          const key = row.key.trim();
          if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(key)) throw new Error("参数键须以英文字母开头，可包含字母、数字、下划线及短横线，最多 64 个字符。");
          if (reservedCountKeys.has(key) || [row.request_key, parameters[key]?.request_key].some(value => reservedCountKeys.has(String(value || "").trim()))) throw new Error("count 和 n 保留给本次总张数，请为工作流输入使用其他参数键及 request_key。");
          if (Object.prototype.hasOwnProperty.call(bindings, key)) throw new Error(`参数键 ${key} 重复。`);
          if (!row.targets?.length) throw new Error(`请为 ${row.label || key} 选择至少一个节点输入。`);
          for (const target of row.targets) {
            const identity = JSON.stringify([String(target.node_id), target.input_name]);
            if (occupied.has(identity)) throw new Error(`节点 #${target.node_id} 的 ${target.input_name} 已由“${occupied.get(identity)}”使用，不能再绑定到“${row.label || key}”。`);
            occupied.set(identity, row.label || key);
          }
          if (row.mode === "append" && !["text", "textarea"].includes(row.type || "text")) throw new Error(`${row.label || key} 只有文本输入才能使用追加方式。`);
          bindings[key] = { ...row, targets: row.targets.map(target => ({ ...target, class_type: definition.api_graph[target.node_id]?.class_type })), type: row.type === "integer" ? "number" : row.type === "textarea" ? "text" : row.type || "text", source: row.source || "parameter", mode: row.mode || "replace", ...(row.source === "reference" ? { reference_index: row.reference_index || 0 } : {}) };
          delete bindings[key].key;
        }
        const outputs = Array.from($("comfyOutputs")?.selectedOptions || []).map(item => item.value);
        const result = { ...definition, bindings, outputs, execution_policy: executionPolicy };
        delete result.parameters_schema;
        return result;
      };
      const parameterSchema = comfyui => {
        // Retain unrelated historical schema entries and each surviving
        // binding's policies, while dropping parameters whose binding was removed.
        const result = totalParameters(Object.fromEntries(Object.entries(parameters).filter(([key]) => !originalBindingKeys.has(key))));
        for (const row of rows) {
          if (["prompt", "negative_prompt", "reference"].includes(row.source)) continue;
          const target = row.targets[0], node = comfyui.api_graph[target?.node_id], value = node?.inputs?.[target?.input_name];
          const spec = info[node?.class_type]?.input, inputSpec = spec?.required?.[target?.input_name] || spec?.optional?.[target?.input_name];
          result[row.key] = { ...parameters[row.key], type: row.type || "text", label: row.label || row.key, request_key: row.key, default: parameters[row.key]?.default ?? value ?? "", ...(Array.isArray(inputSpec?.[0]) ? { choices: inputSpec[0] } : {}), ...(row.source === "seed" ? { type: "integer", min: -1, step: 1 } : {}) };
        }
        return result;
      };
      const renderSeedWarnings = () => {
        const host = $("comfySeedWarnings"); if (!host) return;
        const signature = JSON.stringify(seedWarnings);
        host.innerHTML = signature === dismissedSeedSignature ? "" : seedNoticeMarkup(seedWarnings);
        host.querySelector("[data-dismiss-seed-warning]")?.addEventListener("click", () => { dismissedSeedSignature = signature; host.replaceChildren(); });
      };
      const scheduleSeedWarnings = () => {
        clearTimeout(seedTimer); const current = ++seedRevision;
        seedTimer = setTimeout(async () => {
          if (!alive || !hasWorkflow()) return;
          try {
            const comfyui = snapshot();
            const payload = await apiPost("comfy/import", { comfyui, parameters: parameterSchema(comfyui) });
            if (alive && current === seedRevision) { seedWarnings = payload.seed_warnings || []; renderSeedWarnings(); }
          } catch { /* Incomplete inputs can be corrected without interrupting editing. */ }
        }, 220);
      };
      const clearReport = () => { report = null; if ($("comfyCompatibility")) $("comfyCompatibility").innerHTML = '<p class="field-hint">工作流有修改，应用或生成前将重新检查。</p>'; scheduleSeedWarnings(); };
      const render = () => {
        syncEditorState();
        const choices = inputChoices();
        $("comfyNodeCount").textContent = `${Object.keys(definition.api_graph || {}).length} 个节点`;
        $("comfyBindings").innerHTML = rows.map((row, index) => {
          const targets = selectedTargets(row);
          const targetOptions = choices.map(item => {
            const value = JSON.stringify([item.id, item.name]), owner = targetOwner(value, index);
            const label = item.title + (owner >= 0 ? `（已由 ${rows[owner].label || rows[owner].key} 使用）` : "");
            return option(value, label, targets.has(value), owner >= 0 && !targets.has(value));
          }).join("");
          return `<section class="comfy-binding" data-comfy-binding="${index}"><div class="comfy-binding-heading"><strong>输入 ${index + 1}</strong><button type="button" class="studio-icon-button" data-binding-remove="${index}" aria-label="删除输入 ${index + 1}" data-studio-icon="X"></button></div><div class="comfy-binding-fields"><label class="field">参数键<input data-binding-field="key" value="${escape(row.key)}" spellcheck="false" /></label><label class="field">显示名称<input data-binding-field="label" value="${escape(row.label)}" /></label><label class="field">输入来源<select data-binding-field="source">${Object.entries(sources).map(([value, title]) => option(value, title, row.source === value)).join("")}</select></label><label class="field">数据类型<select data-binding-field="type">${["text", "textarea", "integer", "number", "boolean", "select", "image", "mask"].map(value => option(value, ({ text: "文本", textarea: "多行文本", integer: "整数", number: "数字", boolean: "开关", select: "选项", image: "图片", mask: "蒙版" })[value], (row.type || "text") === value)).join("")}</select></label><label class="field comfy-targets">写入节点输入<select multiple data-binding-targets aria-label="输入 ${index + 1} 的绑定目标">${targetOptions}</select></label>${row.source === "reference" ? `<label class="field">参考图序号<input type="number" min="1" max="8" data-binding-field="reference_index" value="${(row.reference_index || 0) + 1}" /></label>` : `<label class="field">写入方式<select data-binding-field="mode">${option("replace", "替换", row.mode !== "append")}${option("append", "追加文本", row.mode === "append")}</select></label>`}</div></section>`;
        }).join("") || '<p class="field-hint">尚未添加输入，将按工作流原值执行。点击“添加输入”选择已识别的字段，或使用“自定义”手动配置。</p>';
        $("comfyBindings").querySelectorAll("[data-binding-remove]").forEach(button => button.addEventListener("click", () => { preserveRows(); rows.splice(Number(button.dataset.bindingRemove), 1); clearReport(); render(); }));
        $("comfyBindings").querySelectorAll("input,select").forEach(input => input.addEventListener("change", () => { preserveRows(); clearReport(); if (["source", "type"].includes(input.dataset.bindingField)) render(); else refreshAvailability(); }));
        const outputSet = new Set((definition.outputs || []).map(String));
        $("comfyOutputs").innerHTML = Object.entries(definition.api_graph || {}).map(([id, node]) => option(id, nodeTitle(id, node), outputSet.has(id))).join("");
        $("comfyOutputs").onchange = () => { definition.outputs = Array.from($("comfyOutputs").selectedOptions).map(item => item.value); clearReport(); void identifyInputs(); };
        $("comfyFixedInputs").innerHTML = Object.entries(definition.api_graph || {}).map(([id, node]) => {
          const fields = choices.filter(item => item.id === id);
          if (!fields.length) return "";
          return `<details class="comfy-node"><summary>${escape(nodeTitle(id, node))}</summary><div class="comfy-binding-fields">${fields.map(item => {
            const spec = info[node.class_type]?.input, schema = spec?.required?.[item.name] || spec?.optional?.[item.name], options = Array.isArray(schema?.[0]) ? schema[0] : report?.models?.find(model => String(model.node_id) === id && model.input_name === item.name)?.options;
            const attributes = `data-fixed-node="${escape(id)}" data-fixed-input="${escape(item.name)}"`;
            const input = options ? `<select ${attributes} data-fixed-options="${escape(JSON.stringify(options))}">${options.includes(item.value) ? "" : option(item.value, `${item.value}（服务器未提供）`, true)}${options.map(value => option(value, value, value === item.value)).join("")}</select>` : typeof item.value === "boolean" ? `<select ${attributes} data-fixed-type="boolean">${option("true", "开启", item.value)}${option("false", "关闭", !item.value)}</select>` : typeof item.value === "number" ? `<input type="number" ${attributes} value="${escape(item.value)}" />` : `<textarea rows="2" ${attributes}>${escape(item.value ?? "")}</textarea>`;
            return `<label class="field">${escape(item.name)}${input}</label>`;
          }).join("")}</div></details>`;
        }).join("");
        $("comfyFixedInputs").querySelectorAll("[data-fixed-input]").forEach(input => input.addEventListener("change", () => {
          preserveRows();
          const node_id = input.dataset.fixedNode, input_name = input.dataset.fixedInput;
          const value = input.dataset.fixedOptions ? JSON.parse(input.dataset.fixedOptions).find(value => String(value) === input.value) : input.type === "number" ? numericValue(input.value) : input.dataset.fixedType === "boolean" ? input.value === "true" : input.value;
          const binding = rows.find(row => row.targets?.some(target => target.node_id === node_id && target.input_name === input_name));
          if (binding) {
            binding.default = value;
            if (parameters[binding.key]) parameters[binding.key].default = value;
          }
          // One exposed parameter may feed several nodes. Keep its default
          // and every bound literal aligned when replacing a model or value.
          for (const target of binding?.targets || [{ node_id, input_name }]) {
            definition.api_graph[target.node_id].inputs[target.input_name] = value;
            definition.input_overrides = [...(definition.input_overrides || []).filter(item => item.node_id !== target.node_id || item.input_name !== target.input_name), { ...target, value }];
            $("comfyFixedInputs").querySelectorAll("[data-fixed-input]").forEach(element => { if (element.dataset.fixedNode === target.node_id && element.dataset.fixedInput === target.input_name) element.value = String(value); });
          }
          clearReport();
        }));
        $("comfyCompatibility").innerHTML = report ? issuesMarkup(report) : `<p class="field-hint">${options.temporary ? "应用到生图页面前必须通过兼容性检查。参考图可在生图页面补充。" : "可先保存工作流，执行前必须通过依赖检查。"}</p>`;
        renderSeedWarnings();
        $("comfyEditor").querySelectorAll("[data-studio-icon]").forEach(element => {
          const icons = window.StudioIcons;
          if (icons?.[element.dataset.studioIcon]) element.replaceChildren(icons.createElement(icons[element.dataset.studioIcon], { width: 18, height: 18, "aria-hidden": "true", "stroke-width": 1.8 }));
        });
        refreshAvailability();
      };
      const importContent = async file => {
        if (busy) return;
        busy = true; const requestRevision = ++revision;
        syncEditorState();
        $("comfyEditor").querySelector(".comfy-import").open = true;
        $("comfyImportStatus").textContent = "正在读取工作流…";
        if (file) $("comfyImportStatus").scrollIntoView({ block: "nearest" });
        try {
          const payload = file ? await (await bridge()).upload("comfy/import", file) : await apiPost("comfy/import", { content: $("comfyImportJSON").value });
          if (!alive || requestRevision !== revision) return;
          definition = displayDefinition(payload.comfyui); definition.bindings = {}; delete definition.parameters_schema;
          originalBindingKeys = new Set();
          parameters = totalParameters({ count: parameters.count }); report = null; info = {};
          seedRevision++; seedWarnings = payload.seed_warnings || [];
          if (!definition.outputs?.length) definition.outputs = payload.outputs || [];
          identificationRevision++; identifying = false; candidates = suggestedRows(payload.suggestions); rows = [];
          $("comfyImportStatus").textContent = "已读取，可按需添加输入，并确认结果节点后保存。";
          render();
        } catch (error) { if (alive) $("comfyImportStatus").textContent = errorText(error); }
        finally { busy = false; if (alive) syncEditorState(); }
      };
      const body = `<div id="comfyEditor" class="comfy-editor"><p class="field-hint">目标 ComfyUI：${escape(provider.name || provider.id)}</p><label class="field">工作流名称<input id="comfyWorkflowName" value="${escape(model.name || "新工作流")}" /></label><div class="comfy-binding-fields"><label class="field">工作流单次出图张数<input id="comfyNativeBatch" type="number" min="1" max="16" step="1" value="${escape(original.native_batch_size || 1)}" /></label><label class="field">工作流最大并发请求数<input id="comfyConcurrency" type="number" min="1" max="16" step="1" value="${escape(original.max_concurrent_requests || 8)}" /></label></div><p class="field-hint">按单次出图张数安排轮次。超出计划的图片截断，数量不足不补跑。</p><details class="comfy-import"${Object.keys(definition.api_graph || {}).length ? "" : " open"}><summary>导入 API 工作流或原始图片</summary><div class="comfy-import-content"><p class="field-hint">可将图片或 JSON 文件拖入浮窗任意位置。图片需包含 prompt 执行图；仅有界面 workflow 时请先在 ComfyUI 导出 API 格式。</p><button type="button" class="quiet-button" id="comfyChooseFile">选择 JSON / 图片</button><input type="file" id="comfyImportFile" accept=".json,image/png,image/webp,image/jpeg" hidden /><label class="field">或粘贴 API JSON<textarea id="comfyImportJSON" rows="5" spellcheck="false"></textarea></label><button type="button" class="quiet-button" id="comfyReadJSON">读取 JSON</button><p id="comfyImportStatus" class="field-hint" role="status"></p></div></details><div id="comfySeedWarnings"></div><div class="field-label-row"><h3>可调整输入</h3><span id="comfyNodeCount" class="field-hint"></span><button id="comfyAddBinding" type="button" class="quiet-button">添加输入</button></div><div id="comfyBindings"></div><label class="field">收集结果的节点<select id="comfyOutputs" multiple aria-label="收集结果的节点"></select></label><p class="field-hint">只收集选定节点的图片；保留完整工作流执行。请选择保存或预览图片的节点。</p><details><summary>工作流固定输入与模型替换</summary><p class="field-hint">检查兼容性后可选择服务器已有模型。修改固定输入会改变本次保存的工作流。</p><div id="comfyFixedInputs"></div></details><button type="button" class="quiet-button" id="comfyInspect">检查目标 ComfyUI</button><div id="comfyCompatibility" role="status"></div></div>`;
      return hooks.openModal(options.temporary ? "准备临时工作流" : "配置 ComfyUI 工作流", body, [{ label: "取消", action: () => false }, { label: options.temporary ? "检查并用于本次生图" : "应用工作流", id: "comfyApplyWorkflow", disabled: () => !hasWorkflow() || busy, primary: true, action: async () => {
        if (busy) throw new Error("请等待工作流读取或检查完成。");
        const comfyui = snapshot();
        if (!Object.keys(comfyui.api_graph || {}).length) throw new Error("请先导入 API 工作流。");
        if (!comfyui.outputs.length) throw new Error("请选择至少一个结果节点。");
        const name = $("comfyWorkflowName").value.trim(); if (!name) throw new Error("请填写工作流名称。");
        for (const [id, label] of [["comfyNativeBatch", "工作流单次出图张数"], ["comfyConcurrency", "工作流最大并发请求数"]]) if (!$(id).value || !$(id).checkValidity()) throw new Error(`请填写有效的${label}。`);
        const resultParameters = parameterSchema(comfyui);
        const references = rows.filter(row => row.source === "reference");
        const result = { provider: selectedProvider(), model: { ...original, id: original.id || `workflow_${Date.now().toString(36)}`, name, comfyui, parameters: resultParameters, supports_text2img: !references.length, supports_img2img: references.length > 0, supports_negative_prompt: rows.some(row => row.source === "negative_prompt"), max_reference_images: references.length ? Math.max(...references.map(row => (row.reference_index || 0) + 1)) : 1, capability_source: "workflow", prompt_required: rows.some(row => row.source === "prompt"), count_bound: false, native_batch_size: Number($("comfyNativeBatch").value), max_concurrent_requests: Number($("comfyConcurrency").value) } };
        if (options.temporary) {
          busy = true; syncEditorState();
          try {
            const prepared = await apiPost("comfy/import", { provider_id: result.provider.id, temporary_model: result.model });
            report = await apiPost("comfy/inspect", { provider_id: result.provider.id, comfyui: prepared.comfyui });
            seedRevision++; seedWarnings = prepared.seed_warnings || []; info = report.object_info || {}; render();
            if (!compatible(report)) throw new Error("兼容性检查未通过，请按检查结果修复缺失节点、模型或输入后重试。");
            result.model = prepared.model; result.model_ref = prepared.model_ref;
          } finally { busy = false; syncEditorState(); }
        }
        result.seed_warnings = seedWarnings;
        return result;
      } }], { dismissOutside: false, onClose: () => { alive = false; revision++; seedRevision++; clearTimeout(seedTimer); dragEvents?.abort(); $("studioModal").classList.remove("is-comfy-drop-target"); }, onOpen: () => {
        const addInput = document.createElement("select");
        addInput.id = "comfyAddBinding"; addInput.className = "comfy-add-binding"; addInput.setAttribute("aria-label", "添加输入");
        addInput.dataset.menuLayout = "content"; addInput.dataset.menuAlign = "end";
        $("comfyAddBinding").replaceWith(addInput);
        const editing = document.createElement("section");
        editing.id = "comfyWorkflowEditing"; editing.className = "comfy-workflow-editing";
        const importSection = $("comfyEditor").querySelector(".comfy-import");
        while (importSection.nextElementSibling) editing.appendChild(importSection.nextElementSibling);
        $("comfyEditor").appendChild(editing);
        render();
        if (!imported?.suggestions) void identifyInputs();
        const modal = $("studioModal");
        dragEvents = new AbortController();
        const listenerOptions = { signal: dragEvents.signal };
        let dragDepth = 0;
        const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes("Files") || !!event.dataTransfer?.files?.length;
        const clearDropTarget = () => { dragDepth = 0; modal.classList.remove("is-comfy-drop-target"); };
        modal.addEventListener("dragenter", event => {
          if (!hasFiles(event)) return;
          event.preventDefault(); event.stopPropagation(); dragDepth++;
          modal.classList.add("is-comfy-drop-target");
        }, listenerOptions);
        modal.addEventListener("dragover", event => {
          if (!hasFiles(event)) return;
          event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = busy ? "none" : "copy";
        }, listenerOptions);
        modal.addEventListener("dragleave", event => {
          if (!dragDepth) return;
          event.stopPropagation();
          if (--dragDepth <= 0 || event.relatedTarget && !modal.contains(event.relatedTarget)) clearDropTarget();
        }, listenerOptions);
        modal.addEventListener("drop", event => {
          if (!hasFiles(event)) return;
          event.preventDefault(); event.stopPropagation(); clearDropTarget();
          const files = Array.from(event.dataTransfer.files);
          if (busy) { showNotice("正在读取或检查工作流，请稍后再拖入。", "error"); return; }
          if (files.length !== 1) { showNotice("每次请拖入一张图片或一个 JSON 文件。", "error"); return; }
          void importContent(files[0]);
        }, listenerOptions);
        $("comfyChooseFile").addEventListener("click", () => $("comfyImportFile").click());
        $("comfyImportFile").addEventListener("change", event => { const file = event.target.files[0]; event.target.value = ""; if (file) void importContent(file); });
        $("comfyReadJSON").addEventListener("click", () => void importContent());
        $("comfyAddBinding").addEventListener("change", event => {
          const selected = event.target.value;
          if (!selected || selected === "loading") return;
          preserveRows();
          const candidate = selected === "custom" ? { key: `input_${rows.length + 1}`, label: "新参数", source: "parameter", type: "text", targets: [] } : candidates[Number(selected)];
          if (!candidate || Array.from(selectedTargets(candidate)).some(value => targetOwner(value) >= 0)) { refreshAvailability(); return; }
          const row = structuredClone(candidate), baseKey = row.key;
          for (let suffix = 2; rows.some(item => item.key === row.key); suffix++) row.key = baseKey.slice(0, 63 - String(suffix).length) + "_" + suffix;
          if (row.targets.length) row.default = definition.api_graph[row.targets[0].node_id]?.inputs[row.targets[0].input_name];
          rows.push(row); clearReport(); render();
          $("comfyBindings").lastElementChild?.scrollIntoView({ block: "nearest" });
        });
        $("comfyInspect").addEventListener("click", async event => {
          if (busy) return; const button = event.currentTarget; busy = true; button.disabled = true; syncEditorState();
          try { const comfyui = snapshot(); report = await apiPost("comfy/inspect", { provider: selectedProvider(), comfyui }); if (!alive) return; definition = comfyui; info = report.object_info || {}; if (report.suggested_bindings) { identificationRevision++; identifying = false; candidates = suggestedRows(report.suggested_bindings); } render(); }
          catch (error) { if (alive) $("comfyCompatibility").textContent = errorText(error); }
          finally { busy = false; button.disabled = false; if (alive) syncEditorState(); }
        });
      } });
    }

    async function fromGallery(detail, image) {
      const providers = state.providers.filter(item => item.kind === "comfyui" && item.enabled !== false);
      if (!providers.length) throw new Error("请先在设置中添加并保存 ComfyUI 服务商。");
      const imported = await apiPost("comfy/import", { generation_id: detail.id, image_id: image?.id });
      if (!imported.comfyui || !Object.keys(displayDefinition(imported.comfyui).api_graph || {}).length) throw new Error("图片未包含可执行的 ComfyUI API 工作流。仅有界面工作流时，请先在 ComfyUI 导出 API 格式。");
      if (imported.matched_model_ref && state.models.some(model => model.model_ref === imported.matched_model_ref && model.provider_kind === "comfyui")) return false;
      const available = (imported.providers || providers).map(item => ({ ...providers.find(provider => provider.id === item.id), ...item })).filter(item => providers.some(provider => provider.id === item.id));
      if (!available.length) throw new Error("没有可用的 ComfyUI 服务商，请在设置中填写服务地址并启用后保存。");
      const choice = await hooks.openModal("使用图库工作流", `<div class="comfy-gallery-choice"><label class="field">使用方式<select id="comfyGalleryUse"><option value="temporary" selected>临时使用</option><option value="settings">添加到工作流设置</option></select></label><label class="field">目标 ComfyUI<select id="comfyGalleryProvider">${available.map(item => option(item.id, item.name || item.id, item.id === detail.provider_id)).join("")}</select></label><p id="comfyGalleryUseHint" class="field-hint">仅在当前页面准备生图，不添加到已保存的工作流。刷新页面后清除临时工作流。</p></div>`, [
        { label: "取消", action: () => false },
        { label: "继续", primary: true, id: "comfyGalleryContinue", action: () => ({ mode: $("comfyGalleryUse").value, provider: available.find(item => item.id === $("comfyGalleryProvider").value) }) },
      ], { onOpen: () => $("comfyGalleryUse").addEventListener("change", () => { $("comfyGalleryUseHint").textContent = $("comfyGalleryUse").value === "settings" ? "前往目标服务商的工作流设置。应用后加入草稿，保存全部设置后才生效。" : "仅在当前页面准备生图，不添加到已保存的工作流。刷新页面后清除临时工作流。"; }) });
      if (!choice) return true;
      if (!choice.provider) throw new Error("请选择一个已启用的 ComfyUI 服务商。");
      const model = { ...(imported.model || {}), id: `workflow_${Date.now().toString(36)}`, name: imported.model?.name || detail.model_name || "图库工作流" };
      if (choice.mode === "settings") {
        const provider = await hooks.prepareWorkflowSettings(choice.provider.id);
        const result = await edit(provider, model, imported);
        if (result) hooks.addWorkflowDraft(result);
      } else {
        const result = await edit(choice.provider, model, imported, { temporary: true });
        if (result) applyTemporary(result, imported.references || [], imported.warnings || []);
      }
      return true;
    }

    function renderJobs() {
      const host = $("comfyJobs"), list = $("comfyJobList"); if (!host || !list) return;
      host.hidden = !jobs.size;
      const labels = { created: "准备中", preparing: "准备输入", queued: "排队中", submitting: "提交中", submitted: "已提交", running: "执行中", downloading: "读取结果", finalizing: "保存结果", recovering: "恢复任务状态", completed: "已完成", succeeded: "已完成", success: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消", canceled: "已取消", interrupted: "已中断", submission_unknown: "提交结果待核实", unknown: "任务状态待核实", cancel_requested: "正在取消" };
      const progressLabel = progress => progress?.completed != null && progress?.total ? ` · 分批 ${progress.completed}/${progress.total}` : progress?.value != null && progress?.max ? ` · ${progress.value}/${progress.max}` : "";
      const ordered = Array.from(jobs.values()).sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
      const running = ordered.filter(job => !terminal.has(job.status)), problems = ordered.filter(job => problematic.has(job.status));
      $("comfyJobsSummary").textContent = [`${ordered.length} 项`, running.length ? `进行中 ${running.length}` : "", problems.length ? `异常 ${problems.length}` : ""].filter(Boolean).join(" · ");
      // Keep the details element mounted so refreshing progress never changes
      // the user's disclosure state; all retained errors remain accessible.
      const visible = [...running, ...ordered.filter(job => terminal.has(job.status))];
      list.innerHTML = visible.map(job => `<div class="comfy-job"><div><strong>${escape(job.model_name || job.model || job.workflow_name || "ComfyUI 工作流")}</strong><span role="status">${escape(labels[job.status] || job.status || "处理中")}${escape(progressLabel(job.progress))}</span>${job.error ? `<p class="inline-error">${escape(typeof job.error === "string" ? job.error : job.error.message || JSON.stringify(job.error))}</p>` : ""}</div><div class="comfy-job-actions">${job.result || job.result_available ? `<button class="quiet-button" type="button" data-job-result="${escape(job.id)}">查看结果</button>` : ""}${!terminal.has(job.status) ? `<button class="quiet-button" type="button" data-job-cancel="${escape(job.id)}">取消</button>` : (["failed", "unknown"].includes(job.status) && (job.remote_id || job.prompt_id || job.can_resume) ? `<button class="quiet-button" type="button" data-job-resume="${escape(job.id)}">继续查询</button>` : "") + `<button class="quiet-button" type="button" data-job-dismiss="${escape(job.id)}">清除</button>`}</div></div>`).join("");
      list.querySelectorAll("[data-job-result]").forEach(button => button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const { job } = await apiGet("comfy/jobs", { id: button.dataset.jobResult });
          if (!Array.isArray(job?.result?.images)) throw new Error(job?.error || "结果图暂时不可用，请稍后重试。");
          jobs.set(job.id, job); hooks.viewResult(job.result);
        } catch (error) { showNotice(errorText(error), "error"); }
        finally { button.disabled = false; }
      }));
      list.querySelectorAll("[data-job-cancel]").forEach(button => button.addEventListener("click", async () => {
        button.disabled = true;
        try { const payload = await apiPost("comfy/jobs/cancel", { id: button.dataset.jobCancel }); accept(payload.job); }
        catch (error) { showNotice(errorText(error), "error"); }
        finally { button.disabled = false; }
      }));
      list.querySelectorAll("[data-job-resume]").forEach(button => button.addEventListener("click", async () => {
        button.disabled = true;
        try { const payload = await apiPost("comfy/jobs/resume", { id: button.dataset.jobResume }); accept(payload.job); }
        catch (error) { showNotice(errorText(error), "error"); }
        finally { button.disabled = false; }
      }));
      list.querySelectorAll("[data-job-dismiss]").forEach(button => button.addEventListener("click", async () => {
        const id = button.dataset.jobDismiss;
        if (!terminal.has(jobs.get(id)?.status)) return;
        button.disabled = true;
        try { await apiPost("comfy/jobs/dismiss", { id }); jobs.delete(id); renderJobs(); schedule(); }
        catch (error) { showNotice(errorText(error), "error"); }
        finally { button.disabled = false; }
      }));
    }
    function accept(job) {
      if (!job?.id) return;
      const previous = jobs.get(job.id); jobs.set(job.id, job);
      if (Array.isArray(job.result?.images) && previous && !Array.isArray(previous.result?.images)) { hooks.renderResult(job.result); hooks.invalidateBrowseCache(); }
      renderJobs(); schedule();
    }
    function schedule() {
      clearTimeout(timer);
      if (Array.from(jobs.values()).some(job => !terminal.has(job.status))) timer = setTimeout(() => void poll(), document.hidden ? 7000 : 1400);
    }
    async function poll() {
      if (polling) return;
      polling = true;
      try {
        const pending = Array.from(jobs.values()).filter(job => !terminal.has(job.status));
        for (const job of pending) { const payload = await apiGet("comfy/jobs", { id: job.id }); accept(payload.job); }
      } catch { $("comfyJobs").setAttribute("aria-label", "任务状态暂时无法刷新，正在重试"); }
      finally { polling = false; schedule(); }
    }
    async function restore() {
      try { const payload = await apiGet("comfy/jobs"); for (const job of payload.jobs || []) if (!successful.has(job.status)) accept(job); }
      catch { showNotice("ComfyUI 任务状态暂时无法读取，可稍后刷新页面重试。", "error"); }
    }
    async function submit(request) {
      const payload = await apiPost("comfy/jobs", request);
      if (!payload.job?.id) throw new Error("任务提交结果缺少编号，请检查任务列表，避免重复提交。");
      accept(payload.job);
      if (Array.isArray(payload.job.result?.images)) hooks.renderResult(payload.job.result);
    }
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); void poll(); } });
    return { active, promptRequired, totalParameters, numericValue, configuration, bindConfiguration, edit, fromGallery, renderWorkspace, temporaryModel, restore, submit };
  };
})();
