(function () {
  "use strict";

  // Rules are explicit user declarations. UI suggestions never persist a rule.
  window.ImageStudioNodeRules = function ({ escape, apiGet, apiPost, openModal, showNotice, errorMessage, onChanged }) {
    const $ = (id) => document.getElementById(id);
    const labels = { literal: "字段直接输出", passthrough: "输入原样传递", concat: "按顺序拼接", observer: "读取显示回写" };
    const textPort = (port) => String(port.type || "").toUpperCase() === "STRING" || typeof port.value === "string";
    let generation = 0;

    function candidatesMarkup(parsed, { disabled = false, editing = false } = {}) {
      const candidates = parsed?.normalized?.node_rule_candidates || [];
      if (!candidates.length) return "";
      return `<details class="node-rule-candidates" data-import-section="text-nodes"><summary>文本节点识别 · ${candidates.length} 项</summary><div class="node-rule-candidate-list">${candidates.map((node) => `<button class="node-rule-candidate" type="button" data-node-rule-id="${escape(node.node_id)}" ${disabled || editing ? "disabled" : ""}><strong>${escape(node.node_type)} #${escape(node.node_id)}</strong><span>${escape(node.rule ? "已绑定用户规则 · 点击查看或修改" : node.reason || "尚未识别文本关系 · 点击手动绑定")}</span></button>`).join("")}${editing ? '<p class="field-hint">如需配置文本节点，请在导入页添加原图。当前图组的编辑内容不会受影响。</p>' : ""}</div></details>`;
    }

    function portMarkup(node) {
      const inputs = (node.inputs || []).map((port) => {
        const literal = Object.prototype.hasOwnProperty.call(port, "value");
        const state = port.connected ? "已连接" : literal ? "控件值 · 未连接" : "未连接";
        const value = literal ? `<pre>${escape(port.value === "" ? "（空字符串）" : typeof port.value === "string" ? port.value : JSON.stringify(port.value))}</pre>` : "";
        return `<li><div><code>${escape(port.name)}</code><span>${escape(port.type || "类型未提供")} · ${state}</span></div>${value}</li>`;
      }).join("");
      const outputs = (node.outputs || []).map((port) => `<li><div><code>#${escape(port.index)} ${escape(port.name || "")}</code><span>${escape(port.type || "类型未提供")} · ${port.connected ? "已连接" : "未连接"}</span></div></li>`).join("");
      const widgets = (node.widgets || []).map((widget) => `<li><div><code>控件 #${escape(widget.index)}</code><span>界面回写</span></div><pre>${escape(widget.value === "" ? "（空字符串）" : typeof widget.value === "string" ? widget.value : JSON.stringify(widget.value))}</pre></li>`).join("");
      return `<details class="node-rule-ports" open><summary>完整端口与控件</summary><div class="node-rule-port-columns"><section><h4>输入</h4><ul>${inputs || '<li class="field-hint">未提供输入端口</li>'}</ul></section><section><h4>输出</h4><ul>${outputs || '<li class="field-hint">未提供输出端口</li>'}</ul></section></div>${widgets ? `<h4>可读取的显示控件</h4><ul>${widgets}</ul>` : ""}${node.complete === false ? '<p class="field-hint">图片未提供完整端口信息，不能创建适用于所有同类节点的规则。</p>' : ""}</details>`;
    }

    function resultMarkup(parsed) {
      const normalized = parsed?.normalized || {};
      const statuses = { exact: "直接文本", declared: "按用户规则识别", summary: "组合或多阶段摘要", partial: "部分解析", missing: "未读取到文本" };
      const fields = [["prompt", "正向提示词"], ["negative_prompt", "反向提示词"]].map(([key, label]) => `<section><h4>${label}<span>${escape(statuses[normalized[`${key}_status`]] || "")}</span></h4><pre>${escape(normalized[key] || "（未读取到文本）")}</pre></section>`).join("");
      const stages = (normalized.stages || []).map((stage) => {
        const sources = [...(stage.prompt_sources || []).map((source) => ({ ...source, direction: "正向" })), ...(stage.negative_prompt_sources || []).map((source) => ({ ...source, direction: "反向" }))];
        return `<li>${escape(stage.type)} #${escape(stage.node_id)}${sources.filter((source) => source.kind === "user_rule" || source.origin === "user").map((source) => ` · ${source.direction}：${escape(source.node_type || "节点")} #${escape(source.node_id)}${source.conditioning_node_id ? ` → 编码节点 #${escape(source.conditioning_node_id)}` : ""}`).join("")}</li>`;
      }).join("");
      return `<h3>本图解析预览</h3><p class="field-hint">方向由当前保存分支判定。保存规则后重新识别本批图片，手动填写的参数保持不变。</p>${fields}${stages ? `<h4>采样阶段与规则来源</h4><ul class="node-rule-stage-list">${stages}</ul>` : ""}${parsed?.warnings?.length ? `<div class="import-warnings">${parsed.warnings.map(escape).join("<br>")}</div>` : ""}`;
    }

    async function edit({ node, metadata, width, height, outputNodeId, active = () => true }) {
      const session = ++generation;
      let registry;
      try { registry = await apiGet("imports/node-rules"); }
      catch (error) { if (session === generation) showNotice(errorMessage(error, "文本节点规则读取失败"), "error"); return; }
      if (session !== generation || !active()) return;
      const current = node.rule || {};
      const draft = { operation: current.operation || "literal", inputs: [...(current.inputs || [])], output_port: current.output_port ?? null, delimiter: current.delimiter ?? ", ", strip: current.strip === true, widget_index: current.widget_index ?? null, scope: current.scope || "workflow" };
      const outputs = (node.outputs || []).filter(textPort);
      const inputs = (node.inputs || []).filter(textPort);
      const widgets = (node.widgets || []).filter((entry) => typeof entry.value === "string");
      const typeAllowed = node.type_scope_allowed === true;
      let revision = registry.revision, preview = null, editRevision = 0, pending = false;
      const isActive = () => session === generation && active();
      const body = `<div class="node-rule-editor"><p><strong>${escape(node.node_type)} #${escape(node.node_id)}</strong></p><p>这里只声明已确认的文本关系。规则不会执行节点；无法确定内部处理时，请取消并继续手动填写提示词。</p>${portMarkup(node)}<fieldset class="node-rule-form" id="nodeRuleFields"><label class="field">文本关系<select id="nodeRuleOperation">${Object.entries(labels).map(([value, label]) => `<option value="${value}" ${draft.operation === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><div id="nodeRuleRelation"></div><label class="field">适用范围<select id="nodeRuleScope"><option value="workflow" ${draft.scope === "workflow" ? "selected" : ""}>当前工作流中的这个节点</option><option value="type" ${draft.scope === "type" ? "selected" : ""} ${typeAllowed ? "" : "disabled"}>接口和控制选项一致的同类节点</option></select></label><p class="field-hint">默认仅用于当前工作流。扩展至同类节点会校验完整端口及影响行为的控制选项；有未连接的文本接口或接口不完整时不可扩展。</p></fieldset><section class="node-rule-preview" id="nodeRulePreview" aria-live="polite"><p class="field-hint">先选择明确的文本关系，再预览解析结果。</p></section>${current.id ? '<div class="node-rule-remove"><label><input id="nodeRuleDeleteConfirmed" type="checkbox" />确认删除当前用户规则，后续解析将不再使用它。</label></div>' : ""}</div>`;

      function payload() {
        const rule = { operation: draft.operation, inputs: [...draft.inputs], scope: draft.scope };
        if (draft.operation === "observer") rule.widget_index = draft.widget_index;
        else rule.output_port = draft.output_port;
        if (draft.operation === "concat") { rule.delimiter = draft.delimiter; rule.strip = draft.strip; }
        return { metadata, width, height, output_node_id: outputNodeId || "", node_id: node.node_id, rule };
      }
      function complete() { return isActive() && node.bindable !== false && !!draft.inputs.length && draft.inputs.every(Boolean) && new Set(draft.inputs).size === draft.inputs.length && (draft.operation === "concat" || draft.inputs.length === 1) && (draft.operation === "observer" ? Number.isInteger(draft.widget_index) : Number.isInteger(draft.output_port)); }
      function invalidate() {
        editRevision++; preview = null;
        $("nodeRulePreview").innerHTML = '<p class="field-hint">关系已修改，请重新预览。</p>';
        $("nodeRuleSave").disabled = true;
        $("nodeRulePreviewButton").disabled = !complete();
      }
      function renderRelation() {
        const literal = draft.operation === "literal";
        const available = inputs.filter((port) => literal ? typeof port.value === "string" && !port.connected : port.connected || (String(port.type).toUpperCase() === "STRING" && typeof port.value === "string"));
        const select = (selected, index) => `<div class="node-rule-input-row"><span>${index + 1}</span><select data-node-rule-input="${index}" aria-label="第 ${index + 1} 个文本输入"><option value="">选择${literal ? "文本字段" : "文本输入"}</option>${available.map((port) => `<option value="${escape(port.name)}" ${selected === port.name ? "selected" : ""}>${escape(port.name)}</option>`).join("")}</select>${draft.operation === "concat" ? `<button class="quiet-button" type="button" data-node-rule-remove="${index}" aria-label="移除第 ${index + 1} 个文本输入">移除</button>` : ""}</div>`;
        const entries = draft.inputs.length ? draft.inputs : [""];
        const output = draft.operation === "observer" ? `<label class="field">显示回写控件<select id="nodeRuleWidget"><option value="">选择显示控件</option>${widgets.map((widget) => `<option value="${widget.index}" ${draft.widget_index === widget.index ? "selected" : ""}>控件 #${widget.index}</option>`).join("")}</select></label>` : `<label class="field">文本输出<select id="nodeRuleOutput"><option value="">选择文本输出</option>${outputs.map((port) => `<option value="${port.index}" ${draft.output_port === port.index ? "selected" : ""}>#${port.index} ${escape(port.name || "STRING")}</option>`).join("")}</select></label>`;
        $("nodeRuleRelation").innerHTML = `<div class="field"><span>${draft.operation === "concat" ? "拼接顺序（从上到下）" : literal ? "文本字段" : "文本输入"}</span>${entries.map(select).join("")}${draft.operation === "concat" ? '<button class="quiet-button node-rule-add-input" id="nodeRuleAddInput" type="button">添加文本输入</button>' : ""}</div>${output}${draft.operation === "concat" ? `<label class="field">分隔符<textarea id="nodeRuleDelimiter" rows="2">${escape(draft.delimiter)}</textarea></label><label class="node-rule-check"><input id="nodeRuleStrip" type="checkbox" ${draft.strip ? "checked" : ""} />去除拼接结果首尾空白</label>` : ""}<p class="field-hint">${escape(({ literal: "所选字段原文作为该输出的文本。", passthrough: "仅适用于确认不会修改文本的节点。存在格式化、随机化或其他处理时不能声明为原样传递。", concat: "必须明确所有参与拼接的输入、顺序和分隔符。不会猜测未连接接口或条件分支的作用。", observer: "显示控件必须保存所选输入的实际文本。仅关联同一数据输出的回写，保留来源与冲突检查。" })[draft.operation])}</p>`;
        window.ImageStudioSelect?.refresh($("nodeRuleRelation"));
      }
      async function request(action) {
        if (!isActive() || pending) return;
        pending = true; $("nodeRuleFields").disabled = true;
        try { return await action(); }
        finally { pending = false; if (isActive()) $("nodeRuleFields").disabled = false; }
      }
      const actions = [
        { label: "取消", action: () => false },
        ...(current.id ? [{ label: "删除规则", danger: true, id: "nodeRuleDelete", disabled: () => !$("nodeRuleDeleteConfirmed")?.checked, action: () => request(async () => { await apiPost("imports/node-rules/delete", { rule_id: current.id, revision }); await onChanged?.(); showNotice("文本节点规则已删除。", "success"); return true; }) }] : []),
        { label: "预览解析", id: "nodeRulePreviewButton", disabled: () => !complete(), action: () => request(async () => {
          const version = editRevision, input = payload();
          const result = await apiPost("imports/node-rules/preview", input);
          if (!isActive() || version !== editRevision) return;
          revision = result.revision; preview = { input, version };
          $("nodeRulePreview").innerHTML = resultMarkup(result.parsed);
          $("nodeRulePreview").scrollIntoView({ block: "nearest", behavior: "smooth" });
        }) },
        { label: "保存规则", primary: true, id: "nodeRuleSave", disabled: () => !isActive() || !preview || preview.version !== editRevision, action: () => request(async () => {
          if (!preview || preview.version !== editRevision) return;
          await apiPost("imports/node-rules/save", { ...preview.input, revision });
          await onChanged?.(); showNotice("文本节点规则已保存；手动填写的参数已保留。", "success"); return true;
        }) },
      ];
      return openModal("文本节点绑定", body, actions, { onClose: () => { if (session === generation) generation++; }, onOpen: () => {
        renderRelation();
        if (current.id) {
          $("nodeRuleScope").disabled = true;
          $("nodeRuleScope").parentElement.insertAdjacentHTML("afterend", '<p class="field-hint">已有规则保留当前适用范围。如需调整范围，请先删除此规则，再重新绑定。</p>');
        }
        $("nodeRuleFields").addEventListener("input", (event) => {
          const element = event.target;
          if (element.matches("[data-node-rule-input]")) { const position = Number(element.dataset.nodeRuleInput); while (draft.inputs.length <= position) draft.inputs.push(""); draft.inputs[position] = element.value; }
          else if (element.id === "nodeRuleOutput") draft.output_port = element.value === "" ? null : Number(element.value);
          else if (element.id === "nodeRuleWidget") draft.widget_index = element.value === "" ? null : Number(element.value);
          else if (element.id === "nodeRuleDelimiter") draft.delimiter = element.value;
          else if (element.id === "nodeRuleStrip") draft.strip = element.checked;
          else if (element.id === "nodeRuleScope") draft.scope = element.value;
          else if (element.id === "nodeRuleOperation") { draft.operation = element.value; draft.inputs = []; draft.output_port = null; draft.widget_index = null; draft.delimiter = ""; draft.strip = false; renderRelation(); }
          invalidate();
        });
        $("nodeRuleFields").addEventListener("click", (event) => {
          if (event.target.closest("#nodeRuleAddInput")) { if (!draft.inputs.length) draft.inputs.push(""); draft.inputs.push(""); renderRelation(); invalidate(); }
          const remove = event.target.closest("[data-node-rule-remove]");
          if (remove) { draft.inputs.splice(Number(remove.dataset.nodeRuleRemove), 1); renderRelation(); invalidate(); }
        });
        $("nodeRuleDeleteConfirmed")?.addEventListener("change", () => { $("nodeRuleDelete").disabled = !$("nodeRuleDeleteConfirmed").checked; });
      } });
    }

    async function manage() {
      const session = ++generation;
      let registry;
      try { registry = await apiGet("imports/node-rules"); }
      catch (error) { if (session === generation) showNotice(errorMessage(error, "文本节点规则读取失败"), "error"); return; }
      if (session !== generation) return;
      let pending = false, confirmed = "";
      const body = '<div class="node-rule-manager"><p>用户规则独立保存，取消图片导入不会删除规则。修改规则时，请添加包含相应节点的原图，在“文本节点识别”中打开该节点。</p><div id="nodeRuleList"></div></div>';
      const render = () => {
        $("nodeRuleList").innerHTML = (registry.rules || []).map((rule) => `<article class="node-rule-list-item"><div><strong>${escape(rule.node_type || "文本节点")}</strong><span>${escape(labels[rule.operation] || rule.operation)} · ${rule.scope === "type" ? "符合条件的同类节点" : "当前工作流节点"} · 用户声明</span>${confirmed === rule.id ? '<p class="field-hint">删除后，后续解析将不再使用这条规则。</p>' : ""}</div><button class="${confirmed === rule.id ? "danger-button" : "quiet-button"}" type="button" data-node-rule-delete="${escape(rule.id)}" ${pending ? "disabled" : ""}>${confirmed === rule.id ? "确认删除" : "删除"}</button></article>`).join("") || '<p class="field-hint">还没有用户规则。导入 ComfyUI 图片后，点击可绑定的文本节点提示添加。</p>';
      };
      return openModal("文本节点规则", body, [{ label: "关闭", disabled: () => pending, action: () => true }], { onClose: () => { if (session === generation) generation++; }, onOpen: () => {
        render();
        $("nodeRuleList").addEventListener("click", async (event) => {
          const button = event.target.closest("[data-node-rule-delete]");
          if (!button || pending) return;
          const id = button.dataset.nodeRuleDelete;
          if (confirmed !== id) { confirmed = id; render(); return; }
          pending = true; render();
          try {
            const result = await apiPost("imports/node-rules/delete", { rule_id: id, revision: registry.revision });
            await onChanged?.();
            if (session !== generation) return;
            registry = { ...registry, revision: result.revision, rules: registry.rules.filter((rule) => rule.id !== id) };
            confirmed = ""; showNotice("文本节点规则已删除。", "success");
          } catch (error) { if (session === generation) $("studioModalError").textContent = errorMessage(error, "删除失败，请重新打开规则列表后重试"); }
          finally { pending = false; if (session === generation) render(); }
        });
      } });
    }

    return { candidatesMarkup, edit, manage };
  };
})();
