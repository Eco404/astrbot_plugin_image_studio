(() => {
  "use strict";
  const root = document.documentElement;
  const storageKey = "image-studio:appearance:v1";
  const defaults = Object.freeze({ preference: "system", accentHue: 168, accentSaturation: 38, accentLightness: 50, glassOpacity: 0.68 });
  const modes = [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]];
  const swatches = [[168, "青绿"], [130, "叶绿"], [195, "湖蓝"], [216, "雾蓝"], [345, "蔷薇"], [35, "麦金"]];
  const scheme = matchMedia("(prefers-color-scheme: dark)");
  let settings = { ...defaults };
  let savedBaseline = { ...defaults };
  let local = false;
  let initialized = false;
  let storageRevision = 0;
  let saveChain = Promise.resolve();
  let statusText = "";
  let statusError = false;
  let panel;
  let samplePoint = null;
  let sampleRevision = 0;
  let sampledColor = "";
  let resolveReady;
  const ready = new Promise((resolve) => { resolveReady = resolve; });

  function normalize(value) {
    const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const clamp = (key, min, max) => typeof source[key] === "number" && Number.isFinite(source[key]) ? Math.max(min, Math.min(max, source[key])) : defaults[key];
    return {
      preference: modes.some(([value]) => value === source.preference) ? source.preference : defaults.preference,
      accentHue: clamp("accentHue", 0, 359.999999),
      accentSaturation: clamp("accentSaturation", 0, 100),
      accentLightness: clamp("accentLightness", 0, 100),
      glassOpacity: clamp("glassOpacity", 0.2, 1),
    };
  }

  function hslRgb(hue, saturation, lightness) {
    const s = saturation / 100, l = lightness / 100, a = s * Math.min(l, 1 - l);
    return [0, 8, 4].map((n) => {
      const k = (n + hue / 30) % 12;
      return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
    });
  }

  function colorHex(rgb) { return `#${rgb.map((value) => value.toString(16).padStart(2, "0")).join("")}`; }
  function currentColor() { return colorHex(hslRgb(settings.accentHue, settings.accentSaturation, settings.accentLightness)); }

  function readColor(value) {
    let hex = String(value).trim().replace(/^#/, "");
    if (/^[0-9a-f]{3}$/i.test(hex)) hex = hex.split("").map((digit) => digit + digit).join("");
    if (!/^[0-9a-f]{6}$/i.test(hex)) return null;
    const [r, g, b] = hex.match(/../g).map((part) => parseInt(part, 16) / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b), delta = max - min, l = (max + min) / 2;
    let h = settings.accentHue, s = 0;
    if (delta) {
      h = 60 * ((max === r ? (g - b) / delta + 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4) % 6);
      s = delta / (1 - Math.abs(2 * l - 1));
    }
    return { accentHue: h, accentSaturation: s * 100, accentLightness: l * 100 };
  }

  function luminance(rgb) {
    return rgb.reduce((sum, value, index) => {
      const channel = value / 255;
      return sum + (channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4) * [.2126, .7152, .0722][index];
    }, 0);
  }

  function safeLightness(hue, saturation, start, backgrounds, dark) {
    const targets = backgrounds.map(luminance);
    for (let lightness = start; lightness >= 0 && lightness <= 100; lightness += dark ? 1 : -1) {
      const value = luminance(hslRgb(hue, saturation, lightness));
      if (targets.every((target) => (Math.max(value, target) + .05) / (Math.min(value, target) + .05) >= 4.6)) return lightness;
    }
    return dark ? 100 : 0;
  }

  function apply() {
    const resolved = settings.preference === "system" ? (scheme.matches ? "dark" : "light") : settings.preference;
    if (root.dataset.theme !== resolved) root.dataset.theme = resolved;
    root.dataset.themePreference = settings.preference;
    root.style.colorScheme = resolved;
    root.style.setProperty("--accent-h", String(settings.accentHue));
    root.style.setProperty("--accent-s", `${settings.accentSaturation}%`);
    root.style.setProperty("--glass-opacity", `${settings.glassOpacity * 100}%`);
    // Composite only the extra opacity over the drawer's existing glass layer.
    const opacity = settings.glassOpacity;
    const footerTint = opacity < 1 ? (Math.min(1, opacity + .06) - opacity) / (1 - opacity) : 0;
    root.style.setProperty("--detail-footer-tint-opacity", `${footerTint * 100}%`);
    // Correct text/focus colors independently. Raw selection accents retain the
    // chosen color; button and switch surfaces reuse the navigation palette.
    const dark = resolved === "dark", h = settings.accentHue, s = settings.accentSaturation;
    const backgrounds = dark ? [[22, 33, 28], [48, 55, 51], hslRgb(h, 18, 21)] : [[255, 255, 255], [226, 233, 230], hslRgb(h, s, 90)];
    const accent = safeLightness(h, s, dark ? Math.max(62, settings.accentLightness) : Math.min(30, settings.accentLightness), backgrounds, dark);
    const strong = safeLightness(h, Math.min(100, s + (dark ? 6 : 8)), dark ? Math.max(72, accent) : Math.min(27, accent), backgrounds, dark);
    root.style.setProperty("--accent-l", `${accent}%`);
    root.style.setProperty("--accent-strong-l", `${strong}%`);
    const fill = hslRgb(h, s, settings.accentLightness);
    const value = luminance(fill);
    const contrast = (rgb) => {
      const foreground = luminance(rgb);
      return (Math.max(value, foreground) + .05) / (Math.min(value, foreground) + .05);
    };
    const foreground = [[22, 33, 28], [255, 255, 255], [0, 0, 0]].find((rgb) => contrast(rgb) >= 4.5) || [0, 0, 0];
    root.style.setProperty("--control-accent", colorHex(fill));
    root.style.setProperty("--on-control-accent", colorHex(foreground));
    syncControls();
  }

  function setStatus(text, error = false) {
    statusText = text;
    statusError = error;
    syncControls();
  }

  function finish() {
    if (initialized) return;
    initialized = true;
    window.__imageStudioAppearanceGate?.reveal();
    delete root.dataset.appearancePending;
    root.dataset.appearanceReady = "true";
    resolveReady({ ...settings });
  }

  function syncControls() {
    if (!panel) return;
    panel.querySelectorAll('[name="appearanceMode"]').forEach((input) => { input.checked = input.value === settings.preference; });
    panel.querySelectorAll('[name="appearanceAccent"]').forEach((input) => { input.checked = Number(input.value) === settings.accentHue && settings.accentLightness === 50; });
    const hex = currentColor();
    panel.querySelector("#appearanceColor").value = hex;
    const hexInput = panel.querySelector("#appearanceHex");
    if (document.activeElement !== hexInput && hexInput.getAttribute("aria-invalid") !== "true") hexInput.value = hex;
    for (const [name, value] of [["accentSaturation", Math.round(settings.accentSaturation)], ["glassOpacity", Math.round(settings.glassOpacity * 100)]]) {
      panel.querySelector(`[data-appearance-field="${name}"]`).value = String(value);
      panel.querySelector(`[data-appearance-value="${name}"]`).textContent = `${value}%`;
    }
    const status = panel.querySelector(".appearance-status");
    status.textContent = statusText;
    status.hidden = !statusText;
    status.classList.toggle("is-error", statusError);
  }

  function isDirty() { return JSON.stringify(settings) !== JSON.stringify(savedBaseline); }
  function notifyChange() { window.dispatchEvent(new Event("image-studio-appearance-change")); }

  function acceptSaved(value) {
    const preserveDraft = isDirty();
    savedBaseline = normalize(value);
    if (!preserveDraft) settings = { ...savedBaseline };
    apply();
    notifyChange();
  }

  function save(value = settings) {
    const snapshot = normalize(value);
    // An initialization request started before this save cannot supersede it.
    storageRevision += 1;
    setStatus("");
    saveChain = saveChain.catch(() => {}).then(async () => {
      let persisted = false;
      if (local) {
        try {
          localStorage.setItem(storageKey, JSON.stringify(snapshot));
          persisted = true;
        } catch { local = false; }
      }
      if (!persisted) {
        const bridge = window.AstrBotPluginPage;
        if (!bridge?.apiPost || !bridge?.apiGet) throw new Error("页面通信不可用");
        await bridge.apiPost("appearance", snapshot);
        // A successful response alone cannot prove browser cookie storage works.
        const saved = normalize(await bridge.apiGet("appearance"));
        if (JSON.stringify(saved) !== JSON.stringify(snapshot)) throw new Error("浏览器未保留主题设置");
      }
      // Keep any newer preview edits made while this snapshot was being saved.
      savedBaseline = { ...snapshot };
      setStatus("");
      notifyChange();
      return { ...snapshot };
    }).catch((error) => {
      setStatus("主题已应用，但未保存；请允许浏览器存储后重试。", true);
      throw error;
    });
    return saveChain;
  }

  function update(patch) {
    settings = normalize({ ...settings, ...patch });
    if (panel && ["accentHue", "accentSaturation", "accentLightness"].some((key) => Object.hasOwn(patch, key))) panel.querySelector("#appearanceHex").setAttribute("aria-invalid", "false");
    setStatus("");
    apply();
    notifyChange();
  }

  function discard() {
    update(savedBaseline);
    if (!panel) return;
    closeSampler(false);
    panel.querySelector(".appearance-reset-confirmation").hidden = true;
    panel.querySelector(".appearance-reset").setAttribute("aria-expanded", "false");
    panel.querySelector("#appearanceHex").value = currentColor();
  }

  function icon(name) {
    const library = window.StudioIcons;
    if (!library?.[name]) return "";
    const element = library.createElement(library[name]);
    element.setAttribute("aria-hidden", "true");
    return element.outerHTML;
  }

  function closeSampler(restoreFocus = true) {
    sampleRevision++;
    samplePoint = null; sampledColor = "";
    panel.querySelector("#appearanceSampler").hidden = true;
    const canvas = panel.querySelector("#appearanceSampleCanvas");
    canvas.width = canvas.height = 0;
    canvas.hidden = true;
    panel.querySelector("#appearanceSampleFile").value = "";
    panel.querySelector(".appearance-sample-point").hidden = true;
    if (restoreFocus) {
      const trigger = panel.querySelector("#appearanceEyedropper");
      (trigger.getClientRects().length ? trigger : panel.querySelector("#appearanceColor")).focus();
    }
  }

  function openSampler(choose = true) {
    closeSampler(false);
    panel.querySelector("#appearanceSampler").hidden = false;
    panel.querySelector("#appearanceSampleApply").disabled = true;
    panel.querySelector("#appearanceSampleValue").textContent = "尚未取色";
    panel.querySelector("#appearanceSampleSwatch").style.background = "transparent";
    const select = panel.querySelector("#appearanceSampleChoose");
    select.focus();
    if (choose) panel.querySelector("#appearanceSampleFile").click();
  }

  function sampleCanvas(x, y) {
    const canvas = panel.querySelector("#appearanceSampleCanvas");
    if (!canvas.width || !canvas.height) return;
    samplePoint = { x: Math.max(0, Math.min(canvas.width - 1, Math.floor(x))), y: Math.max(0, Math.min(canvas.height - 1, Math.floor(y))) };
    sampledColor = colorHex([...canvas.getContext("2d").getImageData(samplePoint.x, samplePoint.y, 1, 1).data].slice(0, 3));
    panel.querySelector("#appearanceSampleValue").textContent = sampledColor;
    panel.querySelector("#appearanceSampleSwatch").style.background = sampledColor;
    panel.querySelector("#appearanceSampleApply").disabled = false;
    const marker = panel.querySelector(".appearance-sample-point");
    marker.style.left = `${(samplePoint.x + .5) / canvas.width * 100}%`;
    marker.style.top = `${(samplePoint.y + .5) / canvas.height * 100}%`;
    marker.hidden = false;
  }

  async function loadSampleImage(file) {
    if (!file) return;
    if (!/^image\/(png|jpeg|webp|gif|avif|bmp)$/.test(file.type) || file.size > 30 * 1024 * 1024) {
      setStatus("请选择不超过 30 MB 的 PNG、JPEG、WebP、GIF、AVIF 或 BMP 图片。", true); return;
    }
    const requestRevision = ++sampleRevision;
    const canvas = panel.querySelector("#appearanceSampleCanvas");
    samplePoint = null; sampledColor = ""; canvas.hidden = true; canvas.width = canvas.height = 0;
    panel.querySelector(".appearance-sample-point").hidden = true;
    panel.querySelector("#appearanceSampleApply").disabled = true;
    panel.querySelector("#appearanceSampleValue").textContent = "正在读取图片…";
    const url = URL.createObjectURL(file);
    try {
      const image = new Image(); image.src = url;
      await image.decode();
      if (requestRevision !== sampleRevision) return;
      if (!image.naturalWidth || image.naturalWidth * image.naturalHeight > 80_000_000) throw new Error("图片尺寸过大");
      const scale = Math.min(1, 1024 / Math.max(image.naturalWidth, image.naturalHeight));
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.fillStyle = "#ffffff"; context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      canvas.hidden = false;
      panel.querySelector("#appearanceSampleValue").textContent = "尚未取色";
      setStatus(""); canvas.focus({ preventScroll: true });
    } catch {
      if (requestRevision === sampleRevision) {
        panel.querySelector("#appearanceSampleValue").textContent = "读取失败";
        setStatus("图片无法读取或尺寸过大，请选择其他图片。", true);
      }
    } finally { URL.revokeObjectURL(url); }
  }

  async function pickScreenColor() {
    if (typeof window.EyeDropper !== "function" || !window.isSecureContext) { openSampler(); return; }
    const button = panel.querySelector("#appearanceEyedropper");
    button.disabled = true;
    try {
      const result = await new window.EyeDropper().open();
      const color = readColor(result.sRGBHex);
      if (!color) throw new Error("无法读取颜色");
      update(color);
    } catch (error) {
      if (error.name !== "AbortError") {
        openSampler(false);
        setStatus("屏幕吸色不可用，请选择本地图片取色。", true);
      }
    } finally { button.disabled = false; }
  }

  function mount() {
    panel = document.getElementById("appearanceSettings");
    if (!panel) return;
    panel.setAttribute("aria-labelledby", "appearanceTitle");
    panel.innerHTML = `<div class="section-heading appearance-heading"><h2 id="appearanceTitle">主题与显示</h2><button type="button" class="quiet-button appearance-reset" aria-expanded="false" aria-controls="appearanceResetConfirmation">${icon("RefreshCw")}恢复默认</button></div>
      <fieldset class="appearance-fieldset"><legend>显示模式</legend><div class="appearance-modes">${modes.map(([value, title]) => `<label><input type="radio" name="appearanceMode" value="${value}"><span>${title}</span></label>`).join("")}</div></fieldset>
      <fieldset class="appearance-fieldset"><legend>强调色</legend><div class="appearance-swatches">${swatches.map(([hue, title]) => `<label title="${title}" style="--swatch-h:${hue}"><input type="radio" name="appearanceAccent" value="${hue}" aria-label="${title}"><span>${icon("Check")}</span></label>`).join("")}</div></fieldset>
      <div class="appearance-custom-color"><input type="color" id="appearanceColor" aria-label="自选强调色" title="自选颜色"><input type="text" id="appearanceHex" aria-label="强调色 HEX 色值" maxlength="7" spellcheck="false" autocapitalize="off" autocomplete="off"><button type="button" id="appearanceEyedropper" class="studio-icon-button" aria-label="吸取颜色" title="吸取颜色">${icon("Pipette")}</button></div>
      <section id="appearanceSampler" class="appearance-sampler" aria-label="图片取色" hidden>
        <div class="appearance-sample-head"><button type="button" id="appearanceSampleChoose" class="quiet-button">${icon("ImagePlus")}选择取色图片</button><button type="button" id="appearanceSampleClose" class="studio-icon-button" aria-label="取消图片取色" title="取消取色">${icon("X")}</button></div>
        <input type="file" id="appearanceSampleFile" accept="image/png,image/jpeg,image/webp,image/gif,image/avif,image/bmp" hidden>
        <div class="appearance-sample-image"><canvas id="appearanceSampleCanvas" width="0" height="0" tabindex="0" aria-label="取色图片" hidden></canvas><span class="appearance-sample-point" hidden aria-hidden="true"></span></div>
        <div class="appearance-sample-actions"><span id="appearanceSampleSwatch" aria-hidden="true"></span><output id="appearanceSampleValue" aria-live="polite">尚未取色</output><button type="button" id="appearanceSampleApply" class="quiet-button" disabled>使用颜色</button></div>
      </section>
      <label class="appearance-range"><span>强调色饱和度<output data-appearance-value="accentSaturation"></output></span><input type="range" min="0" max="100" step="1" data-appearance-field="accentSaturation" aria-label="强调色饱和度"></label>
      <label class="appearance-range"><span>玻璃不透明度<output data-appearance-value="glassOpacity"></output></span><input type="range" min="20" max="100" step="1" data-appearance-field="glassOpacity" aria-label="玻璃不透明度"></label>
      <div id="appearanceResetConfirmation" class="appearance-reset-confirmation" role="group" aria-label="恢复默认主题" hidden><span>恢复默认主题？</span><div><button type="button" class="quiet-button" data-appearance-reset="cancel">取消</button><button type="button" class="quiet-button" data-appearance-reset="confirm">恢复</button></div></div>
      <p class="appearance-status" role="status" aria-live="polite"></p>
      <div class="field appearance-gallery-sort"><label for="gallerySort">画廊排序方式</label><select id="gallerySort"><option value="created">按创建时间</option><option value="latest_content">按最新内容</option></select><p class="field-hint">按最新内容时，以图组中最新的图片时间排序。</p></div>`;
    window.dispatchEvent(new CustomEvent("image-studio-display-settings-ready"));
    panel.querySelectorAll('[name="appearanceMode"]').forEach((input) => input.addEventListener("change", () => update({ preference: input.value })));
    panel.querySelectorAll('[name="appearanceAccent"]').forEach((input) => input.addEventListener("change", () => update({ accentHue: Number(input.value), accentLightness: 50 })));
    const colorInput = panel.querySelector("#appearanceColor"), hexInput = panel.querySelector("#appearanceHex");
    colorInput.addEventListener("input", () => update(readColor(colorInput.value)));
    const commitHex = () => {
      const color = readColor(hexInput.value);
      hexInput.setAttribute("aria-invalid", String(!color));
      if (!color) { setStatus("请输入有效的 HEX 色值，例如 #3b82f6。", true); return; }
      update(color); hexInput.value = currentColor();
    };
    hexInput.addEventListener("change", commitHex);
    hexInput.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); commitHex(); } });
    const eyedropper = panel.querySelector("#appearanceEyedropper");
    eyedropper.title = typeof window.EyeDropper === "function" && window.isSecureContext ? "从屏幕吸取颜色" : "从本地图片吸取颜色";
    eyedropper.addEventListener("click", () => void pickScreenColor());
    panel.querySelector("#appearanceSampleChoose").addEventListener("click", () => panel.querySelector("#appearanceSampleFile").click());
    panel.querySelector("#appearanceSampleFile").addEventListener("change", (event) => { void loadSampleImage(event.target.files[0]); event.target.value = ""; });
    panel.querySelector("#appearanceSampleClose").addEventListener("click", () => closeSampler());
    const applySample = () => { if (sampledColor) { update(readColor(sampledColor)); closeSampler(); } };
    panel.querySelector("#appearanceSampleApply").addEventListener("click", applySample);
    const canvas = panel.querySelector("#appearanceSampleCanvas");
    canvas.addEventListener("click", (event) => {
      const rect = canvas.getBoundingClientRect();
      sampleCanvas((event.clientX - rect.left) / rect.width * canvas.width, (event.clientY - rect.top) / rect.height * canvas.height);
    });
    canvas.addEventListener("keydown", (event) => {
      const delta = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
      if (delta) {
        event.preventDefault();
        const point = samplePoint || { x: canvas.width / 2, y: canvas.height / 2 };
        sampleCanvas(point.x + delta[0] * (event.shiftKey ? 10 : 1), point.y + delta[1] * (event.shiftKey ? 10 : 1));
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (!sampledColor) sampleCanvas(canvas.width / 2, canvas.height / 2);
        else applySample();
      }
    });
    panel.querySelector("#appearanceSampler").addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeSampler(); } });
    panel.querySelectorAll("[data-appearance-field]").forEach((input) => {
      input.addEventListener("input", () => update({ [input.dataset.appearanceField]: Number(input.value) / (input.dataset.appearanceField === "glassOpacity" ? 100 : 1) }));
    });
    const reset = panel.querySelector(".appearance-reset");
    const confirmation = panel.querySelector(".appearance-reset-confirmation");
    function closeConfirmation(restoreFocus = false) {
      confirmation.hidden = true;
      reset.setAttribute("aria-expanded", "false");
      if (restoreFocus) reset.focus();
    }
    reset.addEventListener("click", () => {
      confirmation.hidden = !confirmation.hidden;
      reset.setAttribute("aria-expanded", String(!confirmation.hidden));
      if (!confirmation.hidden) confirmation.querySelector("button").focus();
    });
    panel.querySelector('[data-appearance-reset="cancel"]').addEventListener("click", () => closeConfirmation(true));
    panel.querySelector('[data-appearance-reset="confirm"]').addEventListener("click", () => { update(defaults); closeConfirmation(true); });
    document.addEventListener("pointerdown", (event) => { if (!confirmation.hidden && !confirmation.contains(event.target) && !reset.contains(event.target)) closeConfirmation(); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !confirmation.hidden) { event.preventDefault(); closeConfirmation(true); } });
    syncControls();
  }

  async function initialize() {
    const bridge = window.AstrBotPluginPage;
    bridge?.onContext?.(apply);
    bridge?.onThemeChange?.(apply);
    const gate = window.__imageStudioAppearanceGate;
    const startingRevision = storageRevision;
    const onTimeout = () => {
      if (initialized) return;
      if (storageRevision === startingRevision) setStatus("未能读取已保存的主题，暂用当前主题。", true);
      finish();
    };
    // Production starts its deadline before any external script. Standalone
    // consumers without the HTML gate retain the same bounded initialization.
    if (gate?.expired) { onTimeout(); return; }
    if (local) { finish(); return; }
    const timeout = gate ? null : setTimeout(onTimeout, 4000);
    if (gate) gate.ready.then((expired) => { if (expired) onTimeout(); });
    try {
      if (!bridge?.apiGet) throw new Error("页面通信不可用");
      await bridge.ready();
      if (initialized || gate?.expired) return;
      const saved = await bridge.apiGet("appearance");
      if (initialized || gate?.expired) return;
      if (storageRevision === startingRevision) acceptSaved(saved);
      clearTimeout(timeout);
      finish();
    } catch {
      if (storageRevision === startingRevision) setStatus("未能读取已保存的主题，暂用当前主题。", true);
    }
  }

  try {
    const value = localStorage.getItem(storageKey);
    // Detect disabled writes without leaving a second preference key behind.
    let parsed;
    try { parsed = value ? JSON.parse(value) : null; } catch { parsed = null; }
    const normalized = normalize(parsed);
    localStorage.setItem(storageKey, JSON.stringify(normalized));
    settings = normalized;
    savedBaseline = { ...normalized };
    local = true;
  } catch {
    root.dataset.appearancePending = "true";
  }
  apply();
  // The host bridge applies its own theme before notifying listeners. This guard
  // also covers host SDK variants which do not expose an onContext callback.
  new MutationObserver(() => {
    const resolved = settings.preference === "system" ? (scheme.matches ? "dark" : "light") : settings.preference;
    if (root.dataset.theme !== resolved) apply();
  }).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  scheme.addEventListener("change", () => { if (settings.preference === "system") apply(); });
  window.addEventListener("storage", (event) => {
    if (!local || event.key !== storageKey) return;
    let value;
    try { value = event.newValue ? JSON.parse(event.newValue) : null; } catch { value = defaults; }
    storageRevision += 1;
    acceptSaved(value);
  });
  window.ImageStudioAppearance = Object.freeze({ ready, get: () => ({ ...settings }), set: update, isDirty, discard, save, defaults, normalize, saved: () => saveChain });
  // Theme I/O must not wait for the gallery, viewer and editor scripts. Only
  // mounting the settings controls needs the parsed document.
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount, { once: true });
  else mount();
  void initialize();
})();
