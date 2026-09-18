(function () {
  "use strict";

  // Shared metadata presentation; optional callbacks keep detail-copy state private.
  window.ImageStudioMetadataMarkup = function ({ escape, parameterRows, deferredDetailSection }) {
    const { serial } = window.ImageStudioPresentation;
    function promptStatusMarkup(status, prefix = "") {
      const label = { declared: "按用户规则识别", snapshot: "由显示快照识别", summary: "组合或多阶段文本摘要", partial: "部分解析", missing: "未读取到文本" }[status];
      return label ? `<span class="comfy-summary-status">${escape(prefix)}${label}</span>` : "";
    }

    function comfyStageFields(stage) {
      const fields = { ...stage }; delete fields.node_id; delete fields.type;
      for (const key of ["prompt_status", "negative_prompt_status"]) if (fields[key]) fields[key] = ({ exact: "直接文本", declared: "按用户规则识别", snapshot: "由显示快照识别", summary: "可读摘要，非等价条件", partial: "部分解析", missing: "未读取到文本" })[fields[key]] || fields[key];
      return fields;
    }

    function comfyDetailsMarkup(metadata, withCopy = false, lazy = false) {
      if (metadata?.format !== "comfyui") return "";
      const normalized = metadata.normalized || {}; const stages = normalized.stages || [];
      if (!stages.length) return "";
      const rows = (values) => withCopy ? parameterRows(values) : Object.entries(values).map(([key, value]) => `<div class="detail-parameter-row"><div class="detail-parameter-label"><span>${escape(key)}</span></div><pre>${escape(serial(value))}</pre></div>`).join("");
      const outputs = (normalized.outputs || []).map((entry) => `<span>${entry.kind === "save" ? "保存输出" : "预览输出"} #${escape(entry.node_id)}${String(entry.node_id) === String(normalized.selected_output_node) ? " · 摘要分支" : ""} · 阶段 ${(entry.stage_ids || []).map(escape).join("、") || "无"}</span>`).join("");
      const stageMarkup = stages.map((stage, index) => {
        const fields = comfyStageFields(stage);
        const title = `阶段 ${index + 1} · ${escape(stage.type)} #${escape(stage.node_id)}`;
        if (withCopy) return deferredDetailSection("comfy-stage", title, () => `<div class="detail-parameter-grid">${rows(fields)}</div>`).replace("<details ", `<details data-comfy-stage="${escape(stage.node_id)}" `);
        return `<details class="comfy-stage" data-comfy-stage="${escape(stage.node_id)}" ${lazy ? `data-import-deferred="stage" data-stage-index="${index}"` : ""}><summary>${title}</summary>${lazy ? "" : `<div class="detail-parameter-grid">${rows(fields)}</div>`}</details>`;
      }).join("");
      const conditions = normalized.condition_nodes && Object.keys(normalized.condition_nodes).length ? withCopy ? deferredDetailSection("comfy-conditions", "条件组合结构", () => rows({ condition_nodes: normalized.condition_nodes })) : `<details class="comfy-conditions" ${lazy ? 'data-import-deferred="conditions"' : ""}><summary>条件组合结构</summary>${lazy ? "" : rows({ condition_nodes: normalized.condition_nodes })}</details>` : "";
      const sources = stages.flatMap((stage) => [...(stage.prompt_sources || []), ...(stage.negative_prompt_sources || [])]);
      const hasSnapshot = sources.some((source) => source.kind !== "user_rule");
      const hasUserRule = sources.some((source) => source.kind === "user_rule" || source.origin === "user");
      const snapshotNote = hasSnapshot ? '<p class="field-hint">显示快照按节点连线识别，未验证是否为本次执行结果。来源见各阶段的 prompt_sources / negative_prompt_sources。</p>' : "";
      const userRuleNote = hasUserRule ? '<p class="field-hint">部分文本按用户声明的节点规则识别；正反向用途由管线判定。来源见各阶段的 prompt_sources / negative_prompt_sources。</p>' : "";
      return `<details class="comfy-workflow-info"><summary>采样阶段与条件 · ${stages.length} 个阶段</summary>${snapshotNote}${userRuleNote}<div class="comfy-output-list">${outputs}</div>${stageMarkup}${conditions}</details>`;
    }

    return { promptStatusMarkup, comfyDetailsMarkup, comfyStageFields };
  };
})();
