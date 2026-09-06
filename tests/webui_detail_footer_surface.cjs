/* Pixel checks verify the footer tint's effective opacity, not just its CSS declarations. */
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to the isolated WebUI harness.");
const engine = process.env.STUDIO_BROWSER || "chromium";
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "studio-detail-footer-surface-"));
const pixelsScript = String.raw`
import io, json, sys
from PIL import Image, ImageStat
image = Image.open(io.BytesIO(sys.stdin.buffer.read())).convert("RGB")
result = {}
for key, point in json.loads(sys.argv[1]).items():
    x, y = map(round, point)
    patch = image.crop((x - 3, y - 3, x + 4, y + 4))
    result[key] = [round(value, 3) for value in ImageStat.Stat(patch).mean]
print(json.dumps(result))
`;

async function settle(frame) {
  await frame.evaluate(async () => {
    await Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {})));
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function appearance(frame, values) {
  await frame.waitForFunction(() => document.documentElement.dataset.appearanceReady === "true");
  await frame.evaluate(async (values) => {
    await window.ImageStudioAppearance.ready;
    window.ImageStudioAppearance.set(values);
    await window.ImageStudioAppearance.saved();
  }, values);
  await settle(frame);
}

async function geometry(frame) {
  return frame.evaluate(() => {
    const box = (id) => document.getElementById(id).getBoundingClientRect().toJSON();
    const footer = document.getElementById("detailFooter");
    const style = getComputedStyle(footer);
    return { drawer: box("detailDrawer"), body: box("drawerBody"), footer: box("detailFooter"), background: style.backgroundColor, blur: style.backdropFilter || style.webkitBackdropFilter, buttons: Array.from(footer.querySelectorAll("button")).filter((button) => button.getClientRects().length).length, viewport: { width: innerWidth, height: innerHeight } };
  });
}

function assertGeometry(result, name) {
  assert.equal(result.blur, "none", `${name}: footer must reuse the parent's backdrop`);
  assert.ok(result.body.bottom <= result.footer.top + 1, `${name}: content extends underneath the footer`);
  assert.ok(Math.abs(result.footer.bottom - result.drawer.bottom) <= 1, `${name}: footer no longer occupies the drawer bottom`);
  assert.ok(result.footer.x >= 0 && result.footer.right <= result.viewport.width + 1 && result.footer.bottom <= result.viewport.height + 1, `${name}: footer outside viewport`);
  assert.ok(result.buttons >= 5, `${name}: footer actions disappeared`);
}

(async () => {
  const browser = await playwright[engine].launch({ headless: true });
  try {
    for (const width of [1440, 390]) {
      for (const theme of ["light", "dark"]) {
        const name = `${engine}-${width}-${theme}`;
        const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
        page.setDefaultTimeout(12000); const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
        await page.goto(base);
        assert.equal(await page.locator("#studio").count(), 1, "expected the isolated harness");
        const frame = page.frames().find((item) => item.url().includes("/ui/"));
        await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
        await appearance(frame, { preference: theme, glassOpacity: 0.55 });
        await frame.locator('[data-view="gallery"]').click();
        await frame.locator(".gallery-card .gallery-info").first().click();
        await frame.locator("#detailUseReference:not(:disabled)").waitFor();
        await settle(frame);
        const before = await geometry(frame); assertGeometry(before, name);
        await page.screenshot({ path: path.join(output, `${name}-detail.png`) });
        await frame.locator("#drawerBody").evaluate((body) => { body.scrollTop = body.scrollHeight; });
        await settle(frame);
        const scrolled = await geometry(frame); assertGeometry(scrolled, `${name}-scrolled`);
        assert.ok(Math.abs(scrolled.footer.top - before.footer.top) <= 1 && Math.abs(scrolled.footer.height - before.footer.height) <= 1, `${name}: footer moved while its body scrolled`);
        await page.screenshot({ path: path.join(output, `${name}-scrolled.png`) });

        if (engine === "chromium") {
          // Hide foreground content, preserving the actual drawer and footer geometry/CSS.
          const points = await frame.evaluate(() => {
            document.documentElement.style.background = "#354b63";
            document.body.style.background = "#354b63";
            document.querySelector(".app-shell").style.visibility = "hidden";
            const scrim = document.getElementById("scrim");
            scrim.style.background = "transparent"; scrim.style.backdropFilter = "none"; scrim.style.webkitBackdropFilter = "none";
            document.querySelectorAll(".drawer-head > *, #drawerBody > *, #detailFooter > *").forEach((element) => { element.style.visibility = "hidden"; });
            const rect = document.getElementById("detailFooter").getBoundingClientRect();
            return { body: [rect.x + rect.width / 2, rect.top - 25], footer: [rect.x + rect.width / 2, rect.y + rect.height / 2] };
          });
          const samples = [];
          const pixelsAt = (png) => JSON.parse(execFileSync(python, ["-c", pixelsScript, JSON.stringify(points)], { input: png, encoding: "utf8" }));
          for (const opacity of [0.2, 0.55, 0.95, 1]) {
            await appearance(frame, { glassOpacity: opacity });
            const png = await page.screenshot({ path: path.join(output, `${name}-flat-${opacity}.png`) });
            const pixels = pixelsAt(png);
            const effectiveOpacity = Math.min(1, opacity + .06);
            await appearance(frame, { glassOpacity: effectiveOpacity });
            const expected = pixelsAt(await page.screenshot());
            assert.ok(pixels.footer.every((channel, index) => Math.abs(channel - expected.body[index]) <= 2), `${name}/${opacity}: footer should match a single layer at ${effectiveOpacity} ${JSON.stringify({ pixels, expected })}`);
            if (opacity === .55) assert.ok(pixels.body.some((channel, index) => Math.abs(channel - pixels.footer[index]) >= 2), `${name}: footer has no visible opacity distinction`);
            samples.push({ opacity, effectiveOpacity, ...pixels, expectedFooter: expected.body });
          }
          assert.ok(samples[0].footer.some((channel, index) => Math.abs(channel - samples.at(-1).footer[index]) > 20), `${name}: changing opacity did not affect actual footer pixels`);
          console.log(`${name}: effective-opacity pixel samples ${JSON.stringify(samples)}`);
        } else console.log(`${name}: shared-backdrop surface and fixed footer geometry passed; no WebKit backdrop pixel claim`);
        assert.deepEqual(errors, [], `${name}: browser errors`);
        await page.close();
      }
    }
    console.log(`Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
