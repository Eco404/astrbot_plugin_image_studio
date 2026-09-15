(function () {
  "use strict";

  window.ImageStudioComfyUI = function (hooks) {
    const { state, escape, apiGet, apiPost, bridge, showNotice } = hooks;
    const $ = id => document.getElementById(id);
    const sources = { parameter: "普通参数", prompt: "主提示词", negative_prompt: "反向提示词", width: "宽度", height: "高度", seed: "种子", count: "生成张数", reference: "参考图 / 蒙版" };
    const terminal = new Set(["completed", "succeeded", "success", "partial", "failed", "cancelled", "canceled", "interrupted", "submission_unknown", "unknown"]);
    const jobs = new Map();
    let timer = 0, polling = false;
    const active = model => model?.provider_kind === "comfyui";
    const bindingList = definition => Object.values(definition?.bindings || {});
    const promptRequired = model => !active(model) || (model.prompt_required ?? model.comfyui_capabilities?.prompt_required ?? bindingList(model.comfyui).some(binding => binding.source === "prompt"));
    const countBound = model => !active(model) || (model.count_bound ?? model.comfyui_capabilities?.count_bound ?? bindingList(model.comfyui).some(binding => binding.source === "count"));
    const option = (value, title, selected) => `<option value="${escape(value)}"${selected ? " selected" : ""}>${escape(title)}</option>`;
    const nodeTitle = (id, node) => `#${id} · ${node?._meta?.title || node?.class_type || "节点"}`;
    const errorText = error => hooks.errorMessage(error, "工作流操作失败");
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
      return `<section class="comfy-workflow-summary field-wide"><div class="field-label-row"><strong>工作流</strong><button type="button" id="comfyEditWorkflow" class="quiet-button">${Object.keys(definition.api_graph || {}).length ? "编辑工作流" : "导入工作流"}</button></div><p class="field-hint">${Object.keys(definition.api_graph || {}).length} 个节点 · ${Object.keys(definition.bindings || {}).length} 个输入绑定 · ${(definition.outputs || []).length} 个结果节点</p><p class="field-hint">完整保留 API 执行图。仅修改已绑定的输入；未绑定张数时使用工作流原有输出数量。</p><div class="comfy-check-actions"><button type="button" id="comfyCheckWorkflow" class="quiet-button">检查兼容性</button><span id="comfyCheckStatus" role="status"></span></div></section>`;
    }

    function issuesMarkup(report) {
      const issues = [...(report?.issues || []), ...(report?.warnings || []).map(item => typeof item === "string" ? { severity: "warning", message: item } : item)];
      const valid = report?.compatible ?? report?.valid ?? !issues.some(item => item.severity === "error");
      return `<p class="comfy-check-result ${valid ? "is-success" : "is-error"}">${valid ? "已完成检查" : "存在未满足的依赖"}</p>${issues.length ? `<ul class="comfy-issues">${issues.map(issue => `<li>${escape([issue.node_id ? `节点 #${issue.node_id}` : "", issue.input_name || issue.input || "", issue.message || issue.detail || String(issue)].filter(Boolean).join(" · "))}</li>`).join("")}</ul>` : '<p class="field-hint">未发现已知缺失项。动态依赖仍以 ComfyUI 执行校验为准。</p>'}`;
    }

    function bindConfiguration(provider, model) {
      $("comfyEditWorkflow")?.addEventListener("click", async () => {
        const result = await edit(provider, model);
        if (!result || hooks.currentSettingsModel() !== model) return;
        Object.assign(model, result.model);
        hooks.renderModelEditor(); hooks.updateSettingsDirty();
        showNotice("工作流已加入设置草稿，保存全部设置后生效。", "success");
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

    async function edit(provider, model = {}, imported = null, providers = null) {
      const original = structuredClone(model);
      let definition = displayDefinition(imported?.comfyui || model.comfyui || { api_graph: {}, bindings: {}, outputs: [] });
      let parameters = structuredClone(imported?.parameters || model.parameters || {});
      let info = {}, report = null, busy = false, revision = 0, alive = true;
      const suggestedRows = (bindings, descriptors = {}) => Object.entries(bindings || {}).map(([key, binding]) => ({ key, label: descriptors[key]?.label || binding.label || key, ...structuredClone(binding) }));
      let rows = suggestedRows(definition.bindings, parameters);
      if (imported && !rows.length) rows = suggestedRows(imported.suggestions, parameters);
      if (imported && !definition.outputs?.length) definition.outputs = imported.outputs || [];
      const selectedProvider = () => providers?.find(item => item.id === $("comfyProvider")?.value) || provider;
      const inputChoices = () => Object.entries(definition.api_graph || {}).flatMap(([id, node]) => Object.entries(node.inputs || {}).filter(([, value]) => !Array.isArray(value) && (value === null || typeof value !== "object")).map(([name, value]) => ({ id, name, value, title: `${nodeTitle(id, node)} → ${name}` })));
      const selectedTargets = row => new Set((row.targets || []).map(target => JSON.stringify([String(target.node_id), target.input_name])));
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
        const bindings = {};
        for (const row of rows) {
          const key = row.key.trim();
          if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(key)) throw new Error("参数键须以英文字母开头，可包含字母、数字、下划线及短横线，最多 64 个字符。");
          if (Object.prototype.hasOwnProperty.call(bindings, key)) throw new Error(`参数键 ${key} 重复。`);
          if (!row.targets?.length) throw new Error(`请为 ${row.label || key} 选择至少一个节点输入。`);
          if (row.mode === "append" && !["text", "textarea"].includes(row.type || "text")) throw new Error(`${row.label || key} 只有文本输入才能使用追加方式。`);
          if (row.source === "count" && Object.values(bindings).some(binding => binding.source === "count")) throw new Error("本次张数只能配置一个输入；需要同时修改多个节点时，请在该输入中选择多个绑定目标。");
          bindings[key] = { ...row, targets: row.targets.map(target => ({ ...target, class_type: definition.api_graph[target.node_id]?.class_type })), type: row.type === "integer" ? "number" : row.type === "textarea" ? "text" : row.type || "text", source: row.source || "parameter", mode: row.mode || "replace", ...(row.source === "reference" ? { reference_index: row.reference_index || 0 } : {}) };
          delete bindings[key].key;
        }
        const outputs = Array.from($("comfyOutputs")?.selectedOptions || []).map(item => item.value);
        return { ...definition, bindings, outputs };
      };
      const clearReport = () => { report = null; if ($("comfyCompatibility")) $("comfyCompatibility").innerHTML = '<p class="field-hint">工作流有修改，生成前将重新检查。</p>'; };
      const render = () => {
        const choices = inputChoices();
        $("comfyNodeCount").textContent = `${Object.keys(definition.api_graph || {}).length} 个节点`;
        $("comfyBindings").innerHTML = rows.map((row, index) => {
          const targets = selectedTargets(row);
          return `<section class="comfy-binding" data-comfy-binding="${index}"><div class="comfy-binding-heading"><strong>输入 ${index + 1}</strong><button type="button" class="studio-icon-button" data-binding-remove="${index}" aria-label="删除输入 ${index + 1}" data-studio-icon="X"></button></div><div class="comfy-binding-fields"><label class="field">参数键<input data-binding-field="key" value="${escape(row.key)}" spellcheck="false" /></label><label class="field">显示名称<input data-binding-field="label" value="${escape(row.label)}" /></label><label class="field">输入来源<select data-binding-field="source">${Object.entries(sources).map(([value, title]) => option(value, title, row.source === value)).join("")}</select></label><label class="field">数据类型<select data-binding-field="type">${["text", "textarea", "integer", "number", "boolean", "select", "image", "mask"].map(value => option(value, ({ text: "文本", textarea: "多行文本", integer: "整数", number: "数字", boolean: "开关", select: "选项", image: "图片", mask: "蒙版" })[value], (row.type || "text") === value)).join("")}</select></label><label class="field comfy-targets">写入节点输入<select multiple data-binding-targets aria-label="输入 ${index + 1} 的绑定目标">${choices.map(item => { const value = JSON.stringify([item.id, item.name]); return option(value, item.title, targets.has(value)); }).join("")}</select></label>${row.source === "reference" ? `<label class="field">参考图序号<input type="number" min="1" max="8" data-binding-field="reference_index" value="${(row.reference_index || 0) + 1}" /></label>` : `<label class="field">写入方式<select data-binding-field="mode">${option("replace", "替换", row.mode !== "append")}${option("append", "追加文本", row.mode === "append")}</select></label>`}</div></section>`;
        }).join("") || '<p class="field-hint">尚未开放参数，将按原始工作流执行。可添加绑定，或导入后确认自动建议。</p>';
        $("comfyBindings").querySelectorAll("[data-binding-remove]").forEach(button => button.addEventListener("click", () => { preserveRows(); rows.splice(Number(button.dataset.bindingRemove), 1); clearReport(); render(); }));
        $("comfyBindings").querySelectorAll("input,select").forEach(input => input.addEventListener("change", () => { preserveRows(); clearReport(); if (["source", "type"].includes(input.dataset.bindingField)) render(); }));
        const outputSet = new Set((definition.outputs || []).map(String));
        $("comfyOutputs").innerHTML = Object.entries(definition.api_graph || {}).map(([id, node]) => option(id, nodeTitle(id, node), outputSet.has(id))).join("");
        $("comfyOutputs").onchange = () => { definition.outputs = Array.from($("comfyOutputs").selectedOptions).map(item => item.value); clearReport(); };
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
        $("comfyCompatibility").innerHTML = report ? issuesMarkup(report) : '<p class="field-hint">可先保存工作流，执行前必须通过依赖检查。</p>';
        $("comfyEditor").querySelectorAll("[data-studio-icon]").forEach(element => {
          const icons = window.StudioIcons;
          if (icons?.[element.dataset.studioIcon]) element.replaceChildren(icons.createElement(icons[element.dataset.studioIcon], { width: 18, height: 18, "aria-hidden": "true", "stroke-width": 1.8 }));
        });
        window.ImageStudioSelect?.refresh($("comfyEditor"));
      };
      const importContent = async file => {
        if (busy) return;
        busy = true; const requestRevision = ++revision;
        $("comfyImportStatus").textContent = "正在读取工作流…";
        try {
          const payload = file ? await (await bridge()).upload("comfy/import", file) : await apiPost("comfy/import", { content: $("comfyImportJSON").value });
          if (!alive || requestRevision !== revision) return;
          definition = displayDefinition(payload.comfyui); parameters = structuredClone(payload.parameters || {}); report = null;
          if (!definition.outputs?.length) definition.outputs = payload.outputs || [];
          let bindings = definition.bindings || {};
          if (!Object.keys(bindings).length && payload.suggestions) bindings = Array.isArray(payload.suggestions) ? Object.fromEntries(payload.suggestions.map(item => [item.key || item.name, item.binding || item])) : payload.suggestions;
          rows = suggestedRows(bindings, parameters);
          $("comfyImportStatus").textContent = "已读取，请确认输入绑定与结果节点后保存。";
          render();
        } catch (error) { if (alive) $("comfyImportStatus").textContent = errorText(error); }
        finally { busy = false; }
      };
      const body = `<div id="comfyEditor" class="comfy-editor">${providers ? `<label class="field">目标 ComfyUI<select id="comfyProvider">${providers.map(item => option(item.id, item.name || item.id, provider?.id === item.id)).join("")}</select></label>` : ""}<label class="field">工作流名称<input id="comfyWorkflowName" value="${escape(model.name || "新工作流")}" /></label><details class="comfy-import"${Object.keys(definition.api_graph || {}).length ? "" : " open"}><summary>导入 API 工作流或原始图片</summary><p class="field-hint">图片需包含 prompt 执行图；仅有界面 workflow 时请先在 ComfyUI 导出 API 格式。</p><button type="button" class="quiet-button" id="comfyChooseFile">选择 JSON / 图片</button><input type="file" id="comfyImportFile" accept=".json,image/png,image/webp,image/jpeg" hidden /><label class="field">或粘贴 API JSON<textarea id="comfyImportJSON" rows="5" spellcheck="false"></textarea></label><button type="button" class="quiet-button" id="comfyReadJSON">读取 JSON</button><p id="comfyImportStatus" class="field-hint" role="status"></p></details><div class="field-label-row"><h3>可调整输入</h3><span id="comfyNodeCount" class="field-hint"></span><button id="comfyAddBinding" type="button" class="quiet-button">添加输入</button></div><div id="comfyBindings"></div><label class="field">收集结果的节点<select id="comfyOutputs" multiple aria-label="收集结果的节点"></select></label><p class="field-hint">只收集选定节点的图片；保留完整工作流执行。请选择保存或预览图片的节点。</p><details><summary>工作流固定输入与模型替换</summary><p class="field-hint">检查兼容性后可选择服务器已有模型。修改固定输入会改变本次保存的工作流。</p><div id="comfyFixedInputs"></div></details><button type="button" class="quiet-button" id="comfyInspect">检查目标 ComfyUI</button><div id="comfyCompatibility" role="status"></div></div>`;
      return hooks.openModal(providers ? "从图片准备工作流" : "配置 ComfyUI 工作流", body, [{ label: "取消", action: () => false }, { label: providers ? "保存工作流并准备生成" : "应用工作流", primary: true, action: async () => {
        if (busy) throw new Error("请等待工作流读取或检查完成。");
        const comfyui = snapshot();
        if (!Object.keys(comfyui.api_graph || {}).length) throw new Error("请先导入 API 工作流。");
        if (!comfyui.outputs.length) throw new Error("请选择至少一个结果节点。");
        const name = $("comfyWorkflowName").value.trim(); if (!name) throw new Error("请填写工作流名称。");
        const resultParameters = {};
        for (const row of rows) {
          if (["prompt", "negative_prompt", "reference"].includes(row.source)) continue;
          const target = row.targets[0], value = comfyui.api_graph[target.node_id]?.inputs[target.input_name];
          const requestKey = row.source === "count" ? "count" : row.key;
          const targetNode = comfyui.api_graph[target.node_id], spec = info[targetNode.class_type]?.input, inputSpec = spec?.required?.[target.input_name] || spec?.optional?.[target.input_name];
          resultParameters[row.key] = { ...parameters[row.key], type: row.type || "text", label: row.label || row.key, request_key: requestKey, default: parameters[row.key]?.default ?? value ?? "", ...(Array.isArray(inputSpec?.[0]) ? { choices: inputSpec[0] } : {}), ...(row.source === "count" ? { refill_from_history: false } : {}) };
        }
        const references = rows.filter(row => row.source === "reference");
        const result = { provider: selectedProvider(), model: { ...original, id: original.id || `workflow_${Date.now().toString(36)}`, name, comfyui, parameters: resultParameters, supports_text2img: !references.length, supports_img2img: references.length > 0, supports_negative_prompt: rows.some(row => row.source === "negative_prompt"), max_reference_images: references.length ? Math.max(...references.map(row => (row.reference_index || 0) + 1)) : 1, capability_source: "workflow", prompt_required: rows.some(row => row.source === "prompt"), count_bound: rows.some(row => row.source === "count"), native_batch_size: original.native_batch_size || 1, max_concurrent_requests: original.max_concurrent_requests || 8 } };
        if (providers) {
          const saved = await apiPost("comfy/workflows", { provider_id: result.provider.id, model: result.model });
          result.model = saved.model || result.model; result.model_ref = saved.model_ref || `${result.provider.id}:${result.model.id}`;
          result.settings_revision = saved.settings_revision;
        }
        return result;
      } }], { dismissOutside: false, onClose: () => { alive = false; revision++; }, onOpen: () => {
        render();
        $("comfyChooseFile").addEventListener("click", () => $("comfyImportFile").click());
        $("comfyImportFile").addEventListener("change", event => { const file = event.target.files[0]; event.target.value = ""; if (file) void importContent(file); });
        $("comfyReadJSON").addEventListener("click", () => void importContent());
        $("comfyAddBinding").addEventListener("click", () => { preserveRows(); rows.push({ key: `input_${rows.length + 1}`, label: "新参数", source: "parameter", type: "text", targets: [] }); clearReport(); render(); });
        $("comfyProvider")?.addEventListener("change", () => { info = {}; clearReport(); });
        $("comfyInspect").addEventListener("click", async event => {
          if (busy) return; const button = event.currentTarget; busy = true; button.disabled = true;
          try { const comfyui = snapshot(); report = await apiPost("comfy/inspect", { provider: selectedProvider(), comfyui }); if (!alive) return; definition = comfyui; info = report.object_info || {}; render(); }
          catch (error) { if (alive) $("comfyCompatibility").textContent = errorText(error); }
          finally { busy = false; button.disabled = false; }
        });
      } });
    }

    async function fromGallery(detail, image) {
      const providers = state.providers.filter(item => item.kind === "comfyui" && item.enabled !== false);
      if (!providers.length) throw new Error("请先在设置中添加并保存 ComfyUI 服务商。");
      const imported = await apiPost("comfy/import", { generation_id: detail.id, image_id: image?.id });
      const result = await edit(providers[0], { name: detail.model_name || "图库工作流" }, imported, providers);
      if (!result) return;
      hooks.adoptSavedWorkflow(result);
      await hooks.bootstrap();
      const originalText = source => { const binding = bindingList(result.model.comfyui).find(binding => binding.source === source), target = binding?.targets?.[0]; return target ? String(result.model.comfyui.api_graph[target.node_id]?.inputs?.[target.input_name] || "") : ""; };
      hooks.applyDraft({ mode: result.model.supports_img2img ? "img2img" : "text2img", provider_id: result.provider.id, model_ref: result.model_ref, model: result.model.id, prompt: originalText("prompt"), negative_prompt: originalText("negative_prompt"), parameters: {}, notice: "工作流已保存，请确认参数并补充参考图后生成。" });
    }

    function renderJobs() {
      const host = $("comfyJobs"); if (!host) return;
      host.hidden = !jobs.size;
      const labels = { created: "准备中", preparing: "准备输入", queued: "排队中", submitting: "提交中", submitted: "已提交", running: "执行中", downloading: "读取结果", finalizing: "保存结果", recovering: "恢复任务状态", completed: "已完成", succeeded: "已完成", success: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消", canceled: "已取消", interrupted: "已中断", submission_unknown: "提交结果待核实", unknown: "任务状态待核实", cancel_requested: "正在取消" };
      const progressLabel = progress => progress?.completed != null && progress?.total ? ` · 分批 ${progress.completed}/${progress.total}` : progress?.value != null && progress?.max ? ` · ${progress.value}/${progress.max}` : "";
      const ordered = Array.from(jobs.values()).sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
      // Never hide an older running task behind newer completed records.
      const visible = [...ordered.filter(job => !terminal.has(job.status)), ...ordered.filter(job => terminal.has(job.status)).slice(0, 12)];
      host.innerHTML = visible.map(job => `<div class="comfy-job"><div><strong>${escape(job.model_name || job.model || job.workflow_name || "ComfyUI 工作流")}</strong><span role="status">${escape(labels[job.status] || job.status || "处理中")}${escape(progressLabel(job.progress))}</span>${job.error ? `<p class="inline-error">${escape(typeof job.error === "string" ? job.error : job.error.message || JSON.stringify(job.error))}</p>` : ""}</div><div class="comfy-job-actions">${job.result || job.result_available ? `<button class="quiet-button" type="button" data-job-result="${escape(job.id)}">查看结果</button>` : ""}${!terminal.has(job.status) ? `<button class="quiet-button" type="button" data-job-cancel="${escape(job.id)}">取消</button>` : ["failed", "unknown"].includes(job.status) && (job.remote_id || job.prompt_id || job.can_resume) ? `<button class="quiet-button" type="button" data-job-resume="${escape(job.id)}">继续查询</button>` : ""}</div></div>`).join("");
      host.querySelectorAll("[data-job-result]").forEach(button => button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const { job } = await apiGet("comfy/jobs", { id: button.dataset.jobResult });
          if (!Array.isArray(job?.result?.images)) throw new Error(job?.error || "结果图暂时不可用，请稍后重试。");
          jobs.set(job.id, job); hooks.viewResult(job.result);
        } catch (error) { showNotice(errorText(error), "error"); }
        finally { button.disabled = false; }
      }));
      host.querySelectorAll("[data-job-cancel]").forEach(button => button.addEventListener("click", async () => {
        button.disabled = true;
        try { const payload = await apiPost("comfy/jobs/cancel", { id: button.dataset.jobCancel }); accept(payload.job); }
        catch (error) { showNotice(errorText(error), "error"); }
        finally { button.disabled = false; }
      }));
      host.querySelectorAll("[data-job-resume]").forEach(button => button.addEventListener("click", async () => {
        button.disabled = true;
        try { const payload = await apiPost("comfy/jobs/resume", { id: button.dataset.jobResume }); accept(payload.job); }
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
      try { const payload = await apiGet("comfy/jobs"); for (const job of payload.jobs || []) accept(job); }
      catch { showNotice("ComfyUI 任务状态暂时无法读取，可稍后刷新页面重试。", "error"); }
    }
    async function submit(request) {
      const payload = await apiPost("comfy/jobs", request);
      if (!payload.job?.id) throw new Error("任务提交结果缺少编号，请检查任务列表，避免重复提交。");
      accept(payload.job);
      if (Array.isArray(payload.job.result?.images)) hooks.renderResult(payload.job.result);
    }
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); void poll(); } });
    return { active, promptRequired, countBound, numericValue, configuration, bindConfiguration, edit, fromGallery, restore, submit };
  };
})();
