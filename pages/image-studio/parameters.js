/** Shared schema projection; keep the contract aligned with backend/models.py. */
(() => {
function resolveModeParameters(parameters, mode) {
  if (mode !== "text2img" && mode !== "img2img") throw new Error("生成模式必须为 text2img 或 img2img");
  const result = {};
  for (const [name, descriptor] of Object.entries(parameters || {})) {
    if (!descriptor || typeof descriptor !== "object" || Array.isArray(descriptor)) continue;
    if (Array.isArray(descriptor.modes) && !descriptor.modes.includes(mode)) continue;
    const resolved = structuredClone(descriptor);
    const override = resolved.mode_overrides?.[mode];
    delete resolved.mode_overrides;
    if (override && typeof override === "object" && !Array.isArray(override)) {
      for (const [key, value] of Object.entries(override)) {
        if (key !== "modes" && key !== "mode_overrides") resolved[key] = value;
      }
    }
    result[name] = resolved;
  }
  return result;
}

function modelForMode(model, mode) {
  if (!model) return model;
  return {
    ...model,
    parameters: resolveModeParameters(model.parameters, mode),
    ...(model.tool ? { tool: {
      ...model.tool,
      parameters: resolveModeParameters(model.tool.parameters, mode),
    } } : {}),
  };
}

window.ImageStudioParameters = { resolveModeParameters, modelForMode };
})();
