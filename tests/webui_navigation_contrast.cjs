/* Standalone rendering check; no AstrBot harness or persisted user data. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const browsers = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const frontend = path.join(__dirname, "../pages/image-studio");
const styles = ["app.css", "library.css", "appearance.css", "controls.css"]
  .map((name) => fs.readFileSync(path.join(frontend, name), "utf8")).join("\n");
const appearance = fs.readFileSync(path.join(frontend, "appearance.js"), "utf8");

for (const engine of ["chromium", "webkit"]) {
  test(`${engine}: translucent navigation preserves its tone and readable text across backdrops`, async () => {
    const browser = await browsers[engine].launch({ headless: true });
    try {
      for (const width of [1440, 901, 900, 390]) {
        const page = await browser.newPage({ viewport: { width, height: 900 } });
        await page.route("http://studio.test/**", (route) => route.fulfill({
          contentType: "text/html", body: `<!doctype html><html><head><style>${styles}
            *, *::before, *::after { transition: none !important; }
          </style></head><body style="background:var(--page)"><aside class="sidebar glass">
          <nav class="nav-list">
            <button class="nav-item is-active"><span class="nav-icon"><svg viewBox="0 0 24 24" stroke="currentColor"><path d="M2 6h20M2 18h20" /></svg></span><span>设置</span></button>
            <button class="nav-item"><span class="nav-icon">○</span><span>画廊</span></button>
          </nav></aside></body></html>`,
        }));
        await page.goto("http://studio.test/");
        await page.addScriptTag({ content: appearance });
        await page.evaluate(() => window.ImageStudioAppearance.ready);
        const report = await page.evaluate(() => {
          const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
          const context = canvas.getContext("2d");
          const sample = document.createElement("span"); document.body.append(sample);
          const paint = (color, underlay) => {
            context.clearRect(0, 0, 1, 1);
            if (underlay) { context.fillStyle = underlay; context.fillRect(0, 0, 1, 1); }
            context.fillStyle = color; context.fillRect(0, 0, 1, 1);
            return [...context.getImageData(0, 0, 1, 1).data];
          };
          const token = (name) => { sample.style.color = `var(${name})`; return getComputedStyle(sample).color; };
          const color = (rgba) => `rgb(${rgba.slice(0, 3).join(" ")} / ${rgba[3] / 255})`;
          const luminance = (rgb) => rgb.slice(0, 3).reduce((sum, value, i) => {
            const s = value / 255;
            return sum + (s <= .04045 ? s / 12.92 : ((s + .055) / 1.055) ** 2.4) * [.2126, .7152, .0722][i];
          }, 0);
          const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
          const selected = document.querySelector(".is-active"), icon = selected.querySelector(".nav-icon"), inactive = document.querySelector(".nav-item:not(.is-active)");
          const mobile = innerWidth <= 900;
          const issues = [], examples = [];
          let minimum = Infinity, checked = 0;
          for (const glassOpacity of [.2, .68, 1]) for (const preference of ["light", "dark"]) {
            for (const accentHue of [0, 35, 57, 130, 168, 216, 279, 345]) {
              for (const accentSaturation of [0, 38, 100]) {
                for (const accentLightness of [0, 7, 30, 45, 49, 50, 60, 75, 90, 100]) {
                  const settings = { preference, accentHue, accentSaturation, accentLightness, glassOpacity };
                  window.ImageStudioAppearance.set(settings);
                  const textStyle = getComputedStyle(selected), iconStyle = getComputedStyle(icon);
                  const source = token("--control-accent");
                  const fill = paint(source);
                  const textColor = paint(textStyle.color), iconColor = paint(iconStyle.color);
                  const panelFill = color(paint(token("--glass"), token("--page")));
                  const itemFill = paint(textStyle.backgroundColor, panelFill);
                  const iconFill = paint(iconStyle.backgroundColor, mobile ? panelFill : color(itemFill));
                  const visibleFill = mobile ? iconFill : itemFill;
                  const textRatio = contrast(textColor, itemFill);
                  const iconRatio = contrast(iconColor, iconFill);
                  const ratio = mobile ? iconRatio : Math.min(textRatio, iconRatio);
                  minimum = Math.min(minimum, ratio); checked++;
                  if ((!mobile && textRatio < 4.5) || iconRatio < 3) issues.push({ reason: "contrast", settings, textRatio, iconRatio, itemFill, iconFill, iconColor });
                  if (paint(`hsl(${accentHue} ${accentSaturation}% ${accentLightness}%)`).some((channel, i) => Math.abs(channel - fill[i]) > 1)) issues.push({ reason: "source color changed", settings });
                  if (mobile && paint(textStyle.backgroundColor)[3] !== 0) issues.push({ reason: "mobile outer fill", settings });
                  const selectionAlpha = paint(mobile ? iconStyle.backgroundColor : textStyle.backgroundColor)[3] / 255;
                  if (selectionAlpha < .6 || selectionAlpha > .825) issues.push({ reason: "selection is not translucent", settings, selectionAlpha });
                  const soft = paint(preference === "light" ? `hsl(${accentHue} ${accentSaturation}% 90%)` : `hsl(${accentHue} 18% 28%)`);
                  if (visibleFill.some((channel, i) => Math.abs(channel - soft[i]) > 6)) issues.push({ reason: "composed selection drifted from its previous tone", settings, visibleFill, soft });
                  for (const backdrop of ["black", "white"]) {
                    const panel = color(paint(token("--glass"), backdrop));
                    const item = paint(textStyle.backgroundColor, panel);
                    const inner = paint(iconStyle.backgroundColor, mobile ? panel : color(item));
                    if ((!mobile && contrast(textColor, item) < 4.5) || contrast(iconColor, inner) < 3) issues.push({ reason: "backdrop contrast", settings, backdrop, textRatio: contrast(textColor, item), iconRatio: contrast(iconColor, inner) });
                  }
                  if (!mobile && Math.abs(paint(iconStyle.backgroundColor)[3] - 255 * .16) > 1) issues.push({ reason: "desktop inner tint changed", settings });
                  const expectedForeground = paint(token("--accent-strong"));
                  if (textColor[3] !== 255 || iconColor[3] !== 255 || textStyle.opacity !== "1") issues.push({ reason: "foreground faded with selection", settings });
                  if (textColor.some((channel, i) => channel !== expectedForeground[i]) || iconColor.some((channel, i) => channel !== expectedForeground[i])) issues.push({ reason: "text or icon no longer uses the strong theme color", settings, textColor, iconColor });
                  if (getComputedStyle(inactive).color !== token("--muted")) issues.push({ reason: "inactive color changed", settings });
                  if (accentHue === 345 && accentSaturation === 38 && [30, 50, 90].includes(accentLightness)) {
                    examples.push({ ...settings, textColor: textStyle.color, iconColor: iconStyle.color, itemLuminance: luminance(itemFill), iconLuminance: luminance(iconFill), textRatio: mobile ? undefined : textRatio, iconRatio });
                  }
                }
              }
            }
          }
          // Colored themes should retain a tinted foreground when readability permits.
          window.ImageStudioAppearance.set({ preference: "dark", accentHue: 345, accentSaturation: 38, accentLightness: 90 });
          const tinted = paint(getComputedStyle(icon).color).slice(0, 3);
          sample.remove();
          return { checked, minimum, issues: issues.slice(0, 8), tinted, examples };
        });
        assert.deepEqual(report.issues, []);
        assert.ok(new Set(report.tinted).size > 1, "colored backgrounds should preserve colored strokes");
        console.log(JSON.stringify({ engine, width, checked: report.checked, minimum: report.minimum, issues: report.issues }));
        await page.close();
      }
    } finally { await browser.close(); }
  });
}
