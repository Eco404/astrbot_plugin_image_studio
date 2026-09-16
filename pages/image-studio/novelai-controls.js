(function () {
  "use strict";

  const roleNames = { base: "图生图底图", mask: "重绘蒙版", character: "角色参考", style: "风格参考", character_style: "角色与风格参考", vibe: "Vibe 风格参考" };
  const modeNames = { img2img: "基础图生图", precise: "精确参考（角色 / 风格）", vibe: "Vibe Transfer", inpaint: "局部重绘" };
  const parse = (value, fallback = []) => { if (Array.isArray(value)) return value; try { const parsed = JSON.parse(value); return Array.isArray(parsed) ? parsed : fallback; } catch { return fallback; } };
  function capabilities(model) {
    if (model?.novelai_capabilities) return model.novelai_capabilities;
    const v5 = String(model?.id || "").startsWith("nai-diffusion-5-");
    return { precise_reference: !v5, vibe_transfer: !v5, inpainting: true, transparency: v5, max_characters: v5 ? 32 : 6, character_position_grid: v5 ? 0 : 5, inpainting_max_characters: model?.id === "nai-diffusion-5-curated" ? 6 : v5 ? 32 : 6, inpainting_character_position_grid: model?.id === "nai-diffusion-5-curated" ? 5 : v5 ? 0 : 5, reference_modes: v5 ? ["img2img", "inpaint"] : ["img2img", "precise", "vibe", "inpaint"] };
  }

  window.ImageStudioNovelAI = function ({ state, model: selectedModel, escape, schemaParameterTitle, schemaParameterLabel, rerenderReferences, uploadFile, openModal, showNotice }) {
    const active = () => selectedModel()?.provider_kind === "novelai_official";
    const schemaEntry = key => Object.entries(selectedModel()?.parameters || {}).find(([name, descriptor]) => (descriptor.request_key || name) === key);
    const value = (key, fallback) => { const entry = schemaEntry(key); return entry ? state.parameterValues[entry[0]] ?? entry[1].default ?? fallback : fallback; };
    const visible = key => { const entry = schemaEntry(key); return !!entry && entry[1].webui_visible !== false; };
    const inputFor = key => { const entry = schemaEntry(key); return entry && Array.from(document.querySelectorAll("#modelParameters [data-model-parameter]")).find(input => input.dataset.modelParameter === entry[0]); };
    function save(key, next) {
      const entry = schemaEntry(key); if (!entry) return;
      state.parameterValues[entry[0]] = structuredClone(next);
      const input = inputFor(key);
      if (input) {
        input.value = Array.isArray(next) ? JSON.stringify(next) : String(next);
        delete input.dataset.unsetValue; delete input.dataset.nullValue;
      }
    }
    const referenceMode = () => value("reference_mode", "img2img");
    const inpainting = () => state.mode === "img2img" && (referenceMode() === "inpaint" || referenceSettings().some(item => item.type === "mask"));
    function updateSamplerControls(correct = false) {
      const input = inputFor("noise_schedule");
      if (!input) return;
      const choices = capabilities(selectedModel()).sampler_noise_schedules?.[value("sampler", "k_euler_ancestral")];
      if (!choices) return;
      input.closest(".field")?.classList.toggle("is-hidden", choices.length === 0);
      if (!choices.length) return;
      const previous = value("noise_schedule", "karras");
      const selected = choices.includes(previous) || !correct ? previous : choices[0];
      input.innerHTML = (choices.includes(selected) ? "" : `<option value="${escape(selected)}" selected>当前采样器不支持：${escape(selected)}</option>`) + choices.map(choice => `<option value="${escape(choice)}"${choice === selected ? " selected" : ""}>${escape(choice)}</option>`).join("");
      if (selected !== previous) save("noise_schedule", selected);
      window.ImageStudioSelect?.refresh(input);
    }
    function updateApplicableControls() {
      if (!active()) return;
      const infill = inpainting();
      const setVisible = (key, shown) => inputFor(key)?.closest(".field, .toggle-row")?.classList.toggle("is-hidden", !shown);
      // Keep both values mounted so changing reference modes preserves the
      // user's last strengths, while only the applicable control is shown.
      setVisible("strength", !infill);
      setVisible("noise", !infill);
      setVisible("extra_noise_seed", !infill);
      setVisible("inpaint_strength", infill);
      setVisible("straight_alpha", !(infill && selectedModel().id === "nai-diffusion-5-curated"));
    }
    function characterCapabilities() {
      const caps = capabilities(selectedModel());
      return inpainting() ? { ...caps, max_characters: caps.inpainting_max_characters ?? caps.max_characters, character_position_grid: caps.inpainting_character_position_grid ?? caps.character_position_grid } : caps;
    }
    function defaultRole(index) {
      const mode = referenceMode();
      if (mode === "precise") return "character";
      if (mode === "vibe") return "vibe";
      if (mode === "inpaint") return index === 0 ? "base" : index === 1 ? "mask" : "character";
      return index === 0 ? "base" : capabilities(selectedModel()).precise_reference ? "character" : "mask";
    }
    function referenceSettings() {
      const settings = parse(value("reference_settings", []));
      return state.references.map((_, index) => {
        const type = settings[index]?.type || defaultRole(index);
        return { type, strength: .6, fidelity: .6, information_extracted: type === "vibe" && selectedModel()?.id === "nai-diffusion-4-5-full" ? .7 : 1, ...(settings[index] || {}) };
      });
    }
    function roles() {
      const caps = capabilities(selectedModel());
      return ["base", ...(caps.inpainting ? ["mask"] : []), ...(caps.precise_reference ? ["character", "style", "character_style"] : []), ...(caps.vibe_transfer ? ["vibe"] : [])];
    }
    function parameter(name, descriptor, current) {
      if (!active()) return null;
      const key = descriptor.request_key || name;
      if (!["characters", "reference_settings", "reference_mode"].includes(key)) return null;
      const label = schemaParameterLabel(name, descriptor);
      const accessibleLabel = escape(schemaParameterTitle(name, descriptor));
      if (key === "reference_mode") {
        const choices = capabilities(selectedModel()).reference_modes;
        const selected = current || "img2img";
        const fallbackHint = selectedModel().id === "nai-diffusion-5-curated" ? "V5 精选版的局部重绘使用 V4.5 精选版，最多 6 个角色，且不支持透明背景。" : "";
        return `<div class="field field-wide">${label}<select aria-label="${accessibleLabel}" data-model-parameter="${escape(name)}" data-parameter-type="select" data-request-key="reference_mode">${choices.includes(selected) ? "" : `<option value="${escape(selected)}" selected>当前模型不支持：${escape(selected)}</option>`}${choices.map(mode => `<option value="${mode}"${selected === mode ? " selected" : ""}>${modeNames[mode]}</option>`).join("")}</select><span class="field-hint">为新图片预选用途；也可在图片卡片中组合底图、蒙版与参考图。精确参考与 Vibe 不能同时使用。${fallbackHint}</span></div>`;
      }
      const hidden = `<textarea hidden data-model-parameter="${escape(name)}" data-parameter-type="json" data-request-key="${key}">${escape(JSON.stringify(parse(current)))}</textarea>`;
      if (key === "reference_settings") return hidden;
      return `<section class="field field-wide novelai-characters" data-novelai-characters>${hidden}<div class="field-label-row">${label}<button type="button" class="quiet-button" data-novelai-character-add>添加角色</button></div><span class="field-hint" data-novelai-character-hint></span><div data-novelai-character-list></div></section>`;
    }
    function renderCharacters() {
      const host = document.querySelector("[data-novelai-character-list]"); if (!host) return;
      const items = parse(value("characters", []));
      const caps = characterCapabilities(), positions = !!value("use_coords", false);
      document.querySelector("[data-novelai-character-hint]").textContent = `主提示词描述画面和角色互动，每张卡片单独描述一个角色。最多 ${caps.max_characters} 个。`;
      host.innerHTML = items.map((item, index) => {
        const coordinate = axis => caps.character_position_grid ? `<select data-character-field="${axis}" aria-label="角色 ${index + 1} ${axis === "x" ? "横向" : "纵向"}位置">${Array.from({ length: caps.character_position_grid }, (_, pos) => (pos + .5) / caps.character_position_grid).map(pos => `<option value="${pos}"${Math.abs(Number(item[axis] ?? .5) - pos) < .001 ? " selected" : ""}>${pos}</option>`).join("")}</select>` : `<input type="number" min="0" max="1" step="0.01" value="${escape(item[axis] ?? .5)}" data-character-field="${axis}" aria-label="角色 ${index + 1} ${axis === "x" ? "横向" : "纵向"}位置" />`;
        return `<article class="novelai-character-card" data-novelai-character="${index}"><div class="field-label-row"><strong>角色 ${index + 1}</strong><button type="button" class="studio-icon-button" data-character-remove="${index}" aria-label="删除角色 ${index + 1}">×</button></div><label class="field">提示词<textarea rows="2" data-character-field="prompt" aria-label="角色 ${index + 1} 提示词">${escape(item.prompt || "")}</textarea></label><label class="field">反向提示词<textarea rows="2" data-character-field="negative_prompt" aria-label="角色 ${index + 1} 反向提示词">${escape(item.negative_prompt || "")}</textarea></label>${positions ? `<div class="novelai-coordinate-fields"><label class="field">横向（左 → 右）${coordinate("x")}</label><label class="field">纵向（上 → 下）${coordinate("y")}</label></div>` : ""}</article>`;
      }).join("");
      document.querySelector("[data-novelai-character-add]").disabled = items.length >= caps.max_characters;
      host.querySelectorAll("[data-character-field]").forEach(input => {
        const update = () => { const index = Number(input.closest("[data-novelai-character]").dataset.novelaiCharacter), values = parse(value("characters", [])); const key = input.dataset.characterField; values[index][key] = ["x", "y"].includes(key) ? Number(input.value) : input.value; save("characters", values); };
        input.addEventListener("input", update); input.addEventListener("change", update);
      });
      host.querySelectorAll("[data-character-remove]").forEach(button => button.addEventListener("click", () => { const values = parse(value("characters", [])); values.splice(Number(button.dataset.characterRemove), 1); save("characters", values); renderCharacters(); }));
      window.ImageStudioSelect?.refresh(host);
    }
    function bindParameters() {
      if (!active()) return;
      inputFor("sampler")?.addEventListener("change", () => updateSamplerControls(true));
      document.querySelector("[data-novelai-character-add]")?.addEventListener("click", () => { const values = parse(value("characters", [])); if (values.length >= characterCapabilities().max_characters) return; values.push({ prompt: "", negative_prompt: "", x: .5, y: .5 }); save("characters", values); renderCharacters(); });
      inputFor("use_coords")?.addEventListener("change", renderCharacters);
      inputFor("reference_mode")?.addEventListener("change", () => {
        // A deliberate mode choice reassigns image roles; ordinary model
        // rendering and history refill must preserve existing assignments.
        if (visible("reference_settings")) save("reference_settings", []);
        rerenderReferences(); renderCharacters();
      });
      updateSamplerControls(); updateApplicableControls();
      renderCharacters();
    }
    function numberField(index, key, title, current) {
      return `<label class="field">${title}<input type="number" min="0" max="1" step="0.05" data-novelai-reference-setting="${key}" data-novelai-reference-index="${index}" value="${escape(current)}" /></label>`;
    }
    function renderReferences(host, remove) {
      updateApplicableControls();
      host.classList.toggle("novelai-reference-strip", active() && visible("reference_settings"));
      if (!active() || !visible("reference_settings")) return false;
      const settings = referenceSettings(), allowed = roles();
      host.innerHTML = state.references.map((item, index) => {
        const config = settings[index], precise = ["character", "style", "character_style"].includes(config.type);
        return `<article class="novelai-reference-card"><div class="novelai-reference-preview"><img src="${escape(item.preview_data_url)}" alt="参考图 ${index + 1}" /><button type="button" class="studio-icon-button" data-reference-index="${index}" aria-label="移除参考图 ${index + 1}">×</button><span>${index + 1}</span></div><label class="field">图片用途<select data-novelai-reference-setting="type" data-novelai-reference-index="${index}">${allowed.includes(config.type) ? "" : `<option value="${escape(config.type)}" selected>当前模型不支持：${escape(config.type)}</option>`}${allowed.map(role => `<option value="${role}"${config.type === role ? " selected" : ""}>${roleNames[role]}</option>`).join("")}</select></label>${precise || config.type === "vibe" ? numberField(index, "strength", "参考强度", config.strength) : ""}${precise ? numberField(index, "fidelity", "保真度", config.fidelity) : ""}${config.type === "vibe" ? numberField(index, "information_extracted", "信息提取程度", config.information_extracted) + '<span class="field-hint">首次编码每张消耗 2 Anlas；当前运行期间可复用编码，重启后需重新编码。</span>' : ""}${config.type === "base" && capabilities(selectedModel()).inpainting ? `<button type="button" class="quiet-button" data-novelai-paint-mask="${index}"${state.references.length >= Number(selectedModel().max_reference_images || 1) && !settings.some(setting => setting.type === "mask") ? " disabled" : ""}>绘制重绘蒙版</button>` : ""}${config.type === "mask" ? '<span class="field-hint">白色区域重绘，黑色区域保留。尺寸应与底图一致。</span>' : ""}</article>`;
      }).join("");
      host.querySelectorAll("[data-reference-index]").forEach(button => button.addEventListener("click", () => { const index = Number(button.dataset.referenceIndex); const values = referenceSettings(); values.splice(index, 1); save("reference_settings", values); remove(index); }));
      host.querySelectorAll("[data-novelai-reference-setting]").forEach(input => {
        const update = () => {
          const values = referenceSettings(), key = input.dataset.novelaiReferenceSetting;
          values[Number(input.dataset.novelaiReferenceIndex)][key] = key === "type" ? input.value : Number(input.value);
          save("reference_settings", values);
          if (key === "type") {
            if (input.value === "mask") { save("reference_mode", "inpaint"); window.ImageStudioSelect?.refresh(inputFor("reference_mode")); }
            rerenderReferences(); renderCharacters();
          }
        };
        input.addEventListener("change", update); if (input.type === "number") input.addEventListener("input", update);
      });
      host.querySelectorAll("[data-novelai-paint-mask]").forEach(button => button.addEventListener("click", () => void paintMask(Number(button.dataset.novelaiPaintMask)).catch(error => showNotice(error.message, "error"))));
      window.ImageStudioSelect?.refresh(host);
      return true;
    }
    function collect() {
      if (!active() || state.mode !== "img2img" || !visible("reference_settings")) return;
      save("reference_settings", referenceSettings());
    }
    function validate() {
      if (!active()) return "";
      const chars = parse(value("characters", [])), caps = characterCapabilities();
      if (chars.length > caps.max_characters) return `当前模型最多支持 ${caps.max_characters} 个角色。`;
      const empty = chars.findIndex(item => !String(item.prompt || "").trim());
      if (empty >= 0) return `请填写角色 ${empty + 1} 的提示词，或删除该角色。`;
      if (state.mode !== "img2img") return "";
      if (!caps.reference_modes.includes(referenceMode())) return "当前模型不支持所选参考方式，请重新选择。";
      const settings = referenceSettings(), types = settings.map(item => item.type);
      const invalid = settings.findIndex(item => !roles().includes(item.type));
      if (invalid >= 0) return `当前模型不支持参考图 ${invalid + 1} 的用途，请重新选择。`;
      if (types.filter(type => type === "base").length > 1) return "基础图生图只支持 1 张底图；请为其他图片选择参考用途或移除。";
      if (types.filter(type => type === "mask").length > 1) return "局部重绘只支持 1 张蒙版。";
      if ((referenceMode() === "inpaint" || types.includes("mask")) && (!types.includes("base") || !types.includes("mask"))) return "局部重绘需要 1 张底图与 1 张蒙版。";
      if ((referenceMode() === "inpaint" || types.includes("mask")) && types.includes("vibe")) return "局部重绘不支持 Vibe Transfer，请移除 Vibe 参考或切换参考方式。";
      if (inpainting() && selectedModel().id === "nai-diffusion-5-curated" && value("tag_hint_transparent_background", false)) return "V5 精选版局部重绘使用 V4.5 精选版，请关闭透明背景提示。";
      if (types.includes("vibe") && types.some(type => ["character", "style", "character_style"].includes(type))) return "精确参考与 Vibe Transfer 不能在同一次生成中使用。";
      return "";
    }
    async function paintMask(index) {
      const target = state.references, base = target[index], modelRef = state.selectedModelRef;
      if (!base) return;
      const image = new Image();
      image.src = base.data_url || base.preview_data_url;
      await image.decode();
      // Upload responses include original dimensions. The preview can be
      // smaller; the exported mask still has the exact base-image size.
      const width = Number(base.width) || image.naturalWidth, height = Number(base.height) || image.naturalHeight;
      const mask = document.createElement("canvas"); mask.width = width; mask.height = height;
      const context = mask.getContext("2d"); context.fillStyle = "black"; context.fillRect(0, 0, width, height);
      let canvas, preview, drawing = false, point = null, dirty = false;
      const drawPreview = () => { preview.clearRect(0, 0, width, height); preview.drawImage(image, 0, 0, width, height); preview.globalAlpha = .45; preview.drawImage(mask, 0, 0); preview.globalAlpha = 1; };
      const result = await openModal("绘制重绘蒙版", '<p>涂白需要重绘的区域，黑色区域将保留。也可取消后上传已有的黑白蒙版。</p><div class="novelai-mask-toolbar"><label class="field">画笔大小<input id="novelaiMaskSize" type="range" min="1" max="25" value="6" /></label><button type="button" class="quiet-button" id="novelaiMaskErase" aria-pressed="false">橡皮擦</button><button type="button" class="quiet-button" id="novelaiMaskClear">清空</button></div><canvas id="novelaiMaskCanvas" class="novelai-mask-canvas" aria-label="涂抹需要重绘的区域"></canvas>', [
        { label: "取消", action: () => false },
        { label: "使用蒙版", primary: true, action: async () => {
          if (!dirty) throw new Error("请先涂抹需要重绘的区域。");
          if (state.references !== target || state.selectedModelRef !== modelRef || state.mode !== "img2img") throw new Error("模型或参考图已更改，请重新打开蒙版编辑。");
          if (!referenceSettings().some(item => item.type === "mask") && target.length >= Number(selectedModel().max_reference_images || 1)) throw new Error("参考图数量已达上限，请先移除一张图片，再添加蒙版。");
          const blob = await new Promise(resolve => mask.toBlob(resolve, "image/png"));
          if (!blob) throw new Error("蒙版导出失败，请重试。");
          return uploadFile(new File([blob], "inpaint-mask.png", { type: "image/png" }));
        } },
      ], { onOpen: () => {
        const root = document.getElementById("studioModal");
        canvas = root.querySelector("#novelaiMaskCanvas"); canvas.width = width; canvas.height = height; preview = canvas.getContext("2d"); drawPreview();
        const position = event => { const rect = canvas.getBoundingClientRect(); return { x: (event.clientX - rect.left) * width / rect.width, y: (event.clientY - rect.top) * height / rect.height }; };
        const paint = event => {
          if (!drawing) return; event.preventDefault(); const next = position(event);
          context.strokeStyle = root.querySelector("#novelaiMaskErase").getAttribute("aria-pressed") === "true" ? "black" : "white";
          context.lineWidth = Math.max(width, height) * Number(root.querySelector("#novelaiMaskSize").value) / 100;
          context.lineCap = "round"; context.lineJoin = "round"; context.beginPath(); context.moveTo(point.x, point.y); context.lineTo(next.x, next.y); context.stroke(); point = next; dirty = true; drawPreview();
        };
        canvas.addEventListener("pointerdown", event => { drawing = true; point = position(event); canvas.setPointerCapture(event.pointerId); paint(event); });
        canvas.addEventListener("pointermove", paint);
        for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) canvas.addEventListener(name, () => { drawing = false; point = null; });
        root.querySelector("#novelaiMaskErase").addEventListener("click", event => { const button = event.currentTarget; button.setAttribute("aria-pressed", button.getAttribute("aria-pressed") === "true" ? "false" : "true"); });
        root.querySelector("#novelaiMaskClear").addEventListener("click", () => { context.fillStyle = "black"; context.fillRect(0, 0, width, height); dirty = false; drawPreview(); });
      } });
      if (!result || state.references !== target || state.selectedModelRef !== modelRef) return;
      const settings = referenceSettings(), existing = settings.findIndex(item => item.type === "mask");
      settings[index] = { ...settings[index], type: "base" };
      if (existing >= 0) { target[existing] = result; settings[existing] = { type: "mask" }; }
      else { target.push(result); settings.push({ type: "mask" }); }
      save("reference_mode", "inpaint"); save("reference_settings", settings); window.ImageStudioSelect?.refresh(inputFor("reference_mode")); rerenderReferences(); renderCharacters();
    }
    return { parameter, bindParameters, renderReferences, collect, validate, capabilities };
  };
})();
