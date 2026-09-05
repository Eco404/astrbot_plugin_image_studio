/* Safe synthetic ComfyUI images; use only an isolated tests/webui_harness.py. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to a fresh isolated ComfyUI test harness.");
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-comfyui-browser-"));
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;

const fixtureScript = String.raw`
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw, PngImagePlugin
folder=Path(sys.argv[1]); name=sys.argv[2]; model=f"safe-landscape-{name}.safetensors"
graph={
 "1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":model}},
 "2":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"mountain lake in daylight"}},
 "3":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"soft clouds and calm reflections"}},
 "4":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"blur, watermark"}},
 "5":{"class_type":"ConditioningCombine","inputs":{"conditioning_1":["2",0],"conditioning_2":["3",0]}},
 "6":{"class_type":"EmptyLatentImage","inputs":{"width":640,"height":480,"batch_size":1}},
 "7":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["5",0],"negative":["4",0],"latent_image":["6",0],"seed":1152921504606847099,"steps":28,"cfg":6.5,"sampler_name":"euler_ancestral","scheduler":"karras","denoise":1}},
 "8":{"class_type":"LatentUpscaleBy","inputs":{"samples":["7",0],"upscale_method":"nearest-exact","scale_by":1.5}},
 "9":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["5",0],"negative":["4",0],"latent_image":["8",0],"seed":1152921504606847101,"steps":18,"cfg":4,"sampler_name":"euler","scheduler":"normal","denoise":0.4}},
 "10":{"class_type":"VAEDecode","inputs":{"samples":["9",0],"vae":["1",2]}},
 "11":{"class_type":"SaveImage","inputs":{"images":["10",0],"filename_prefix":"safe_landscape"}},
 "12":{"class_type":"PreviewImage","inputs":{"images":["10",0]}},
 "13":{"class_type":"PreviewImage","inputs":{"images":["14",0]}},
 "14":{"class_type":"VAEDecode","inputs":{"samples":["7",0],"vae":["1",2]}}
}
widgets={"CheckpointLoaderSimple":[model],"CLIPTextEncode":[],"EmptyLatentImage":[640,480,1],"LatentUpscaleBy":["nearest-exact",1.5],"SaveImage":["safe_landscape"]}
nodes=[]; links=[]; next_link=1
for identifier,node in graph.items():
 inputs=[]
 for key,value in node["inputs"].items():
  if isinstance(value,list) and len(value)==2 and str(value[0]) in graph:
   links.append([next_link,int(value[0]),value[1],int(identifier),len(inputs),"*"])
   inputs.append({"name":key,"type":"*","link":next_link}); next_link+=1
 kind=node["class_type"]; values=widgets.get(kind,[])
 if kind=="CLIPTextEncode":values=[node["inputs"]["text"]]
 if kind=="KSampler":
  data=node["inputs"]; values=[data["seed"],"fixed",data["steps"],data["cfg"],data["sampler_name"],data["scheduler"],data["denoise"]]
 nodes.append({"id":int(identifier),"type":kind,"pos":[int(identifier)*20,100],"size":[200,120],"inputs":inputs,"widgets_values":values})
workflow={"last_node_id":14,"last_link_id":next_link-1,"nodes":nodes,"links":links,"groups":[],"config":{},"extra":{"fixture":name},"version":0.4}
workflow_text=json.dumps(workflow,ensure_ascii=False,indent=2)+"\n"
api_text=json.dumps(graph,ensure_ascii=False,indent=1)+"\n"
(folder/f"{name}-workflow.json").write_text(workflow_text)
(folder/f"{name}-api.json").write_text(api_text)
for index in range(2):
 image=Image.new("RGB",(960,720),(177+index*12,208,213)); draw=ImageDraw.Draw(image)
 draw.rectangle((0,420,960,720),fill=(93,145+index*8,144)); draw.polygon([(0,450),(310,160),(640,450)],fill=(111,139,148)); draw.polygon([(420,470),(760,240),(960,470)],fill=(128,159,137)); draw.ellipse((650,90,770,155),fill=(228,236,230))
 metadata=PngImagePlugin.PngInfo();metadata.add_text("prompt",api_text);metadata.add_text("workflow",workflow_text)
 image.save(folder/f"{name}-{index}.png",pnginfo=metadata)
print(json.dumps({"model":model,"files":[str(folder/f"{name}-{i}.png") for i in range(2)],"workflow":str(folder/f"{name}-workflow.json"),"api":str(folder/f"{name}-api.json")}))
`;

async function settle(inner) {
  await inner.evaluate(async () => Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {}))));
}

async function capture(page, inner, name) {
  if (await inner.locator("#appNoticeClose").isVisible()) await inner.locator("#appNoticeClose").click();
  await settle(inner);
  const geometry = await inner.evaluate(() => ({ width: document.documentElement.clientWidth, pageWidth: document.documentElement.scrollWidth }));
  assert.ok(geometry.pageWidth <= geometry.width + 1, `${name}: overflow ${JSON.stringify(geometry)}`);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function api(page, endpoint) {
  const response = await page.request.get(`${apiRoot}/${endpoint}`); assert.ok(response.ok(), `${endpoint}: ${response.status()}`);
  const result = await response.json(); return result.data || result;
}

async function chooseFormat(frame, inner, value) {
  const index = await inner.locator("#detailCopyFormat").evaluate((select, requested) => Array.from(select.options).findIndex((option) => option.value === requested), value);
  assert.ok(index >= 0, `missing ${value} copy format`);
  await frame.locator('.studio-select-trigger[data-select-id="detailCopyFormat"]').click();
  await frame.locator(`.studio-select-menu [data-option-index="${index}"]`).click();
}

async function verifyStages(scope, name) {
  const container = scope.locator(".comfy-workflow-info").first(); await container.waitFor();
  assert.equal(await container.locator("[data-comfy-stage]").count(), 2, `${name}: both samplers must be represented`);
  await container.locator(":scope > summary").click();
  for (const [node, steps] of [["7", "28"], ["9", "18"]]) {
    const direct = container.locator(`[data-comfy-stage="${node}"]`);
    await direct.locator(":scope > summary").click();
    const body = await direct.textContent();
    assert.match(body, new RegExp(steps)); assert.match(body, /mountain lake in daylight/); assert.match(body, /soft clouds and calm reflections/);
    await direct.locator(":scope > summary").click();
  }
  assert.match(await container.locator(".comfy-output-list").textContent(), /保存输出 #11/);
  assert.match(await container.locator(".comfy-output-list").textContent(), /预览输出 #12/);
  assert.match(await container.locator(".comfy-output-list").textContent(), /预览输出 #13/);
  await container.locator('[data-comfy-stage="7"] > summary').click();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const width of [320, 390, 1440]) for (const theme of ["light", "dark"]) {
      const name = `${width}-${theme}`;
      const fixture = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, name], { encoding: "utf8" }));
      const workflowText = fs.readFileSync(fixture.workflow, "utf8"); const apiText = fs.readFileSync(fixture.api, "utf8");
      const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
      page.setDefaultTimeout(12000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      await page.addInitScript(() => { try { Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (content) => { window.__copiedParameters = content; } } }); } catch {} });
      await page.goto(base);
      const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      const inner = page.frames().find((item) => item.url().includes("/ui/")); await inner.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await frame.locator('[data-view="import"]').click();
      const inspectReplies = []; const uploads = []; let submitted;
      page.on("request", (request) => { if (request.url().includes("/imports/prepare")) submitted = request.postDataJSON(); });
      const responses = [];
      page.on("response", (response) => {
        if (response.url().includes("/imports/inspect")) responses.push(response.json().then((body) => inspectReplies.push(body.data || body)));
        if (response.url().includes("/imports/upload/")) responses.push(response.json().then((body) => uploads.push(body.data || body)));
      });
      await frame.locator("#importFiles").setInputFiles(fixture.files);
      await frame.locator("#confirmImportButton:not(:disabled)").waitFor(); await Promise.all(responses);
      assert.equal(inspectReplies.length, 2); assert.equal(uploads.length, 0);
      const normalized = inspectReplies[0].normalized;
      assert.equal(normalized.prompt_status, "summary");
      assert.match(normalized.prompt, /mountain lake in daylight/); assert.match(normalized.prompt, /soft clouds and calm reflections/);
      assert.equal(String(normalized.selected_output_node), "11", "unique save must determine the summary branch");
      assert.equal(normalized.stages.length, 2);
      assert.ok(Object.keys(normalized.condition_nodes || {}).length, "flat conditioning structure missing");
      assert.equal(normalized.outputs.filter((entry) => entry.kind === "preview").length, 2);
      assert.equal(inspectReplies[0].raw.workflow, workflowText); assert.equal(inspectReplies[0].raw.prompt, apiText);
      const firstCard = frame.locator(".import-card").first();
      assert.match(await firstCard.locator('[data-import-field="prompt"]').inputValue(), /mountain lake in daylight/);
      assert.match(await firstCard.locator(".comfy-summary-status").first().textContent(), /摘要/);
      await verifyStages(firstCard, `${name}-import`);
      await capture(page, inner, `${name}-import-stages`);
      await frame.locator('.import-card [data-import-field="prompt"]').nth(1).fill("");
      await frame.locator("#confirmImportButton").click();
      await frame.locator("#importGrid").filter({ hasNot: frame.locator(".import-card") }).waitFor({ state: "attached" }); await Promise.all(responses);
      assert.equal(uploads.length, 2); assert.ok(submitted);
      const automatic = submitted.items[0].overrides; const cleared = submitted.items[1].overrides;
      for (const key of ["prompt", "mode", "generation_engine", "model", "negative_prompt", "parameters"]) assert.equal(Object.hasOwn(automatic, key), false, `untouched automatic ${key} must not become an override`);
      assert.ok(automatic.generated_at > 0, "fallback import date should be preserved"); assert.equal(cleared.prompt, "", "manual clear must survive as an explicit override");
      const first = await api(page, `gallery/detail/${uploads[0].generation_id}?assets=0`);
      const second = await api(page, `gallery/detail/${uploads[1].generation_id}?assets=0`);
      assert.match(first.original_prompt, /mountain lake in daylight/); assert.equal(second.original_prompt, "");
      assert.equal(second.supplemental.overrides.prompt, "");
      assert.match(second.images[0].metadata.normalized.prompt, /mountain lake in daylight/);
      assert.equal(first.images[0].metadata.normalized.prompt_status, "summary");
      assert.equal(first.images[0].metadata.raw.workflow, workflowText);
      await inner.evaluate(() => window.scrollTo(0, 0)); await frame.locator('[data-view="gallery"]').click();
      await frame.locator("#gallerySearch").fill(fixture.model); await frame.locator("#gallerySearch").press("Tab");
      await frame.locator(`[data-gallery-id="${first.id}"]`).waitFor();
      await frame.locator(`[data-gallery-id="${first.id}"] .gallery-info`).click();
      await frame.locator("#drawerBody .comfy-summary-status").first().waitFor();
      await verifyStages(frame.locator("#drawerBody"), `${name}-detail`);
      await capture(page, inner, `${name}-detail-stages`);
      await chooseFormat(frame, inner, "workflow");
      await frame.locator("#detailCopy").click();
      await inner.waitForFunction(() => typeof window.__copiedParameters === "string");
      assert.equal(await inner.evaluate(() => window.__copiedParameters), workflowText, "workflow copying changed the original JSON text or large seed");
      const downloadPromise = page.waitForEvent("download"); await frame.locator("#detailWorkflowDownload").click();
      const download = await downloadPromise; const downloaded = path.join(output, `${name}-downloaded-workflow.json`); await download.saveAs(downloaded);
      assert.equal(fs.readFileSync(downloaded, "utf8"), workflowText, "workflow download must preserve raw JSON exactly");
      await chooseFormat(frame, inner, "comfy_api");
      await inner.evaluate(() => { window.__copiedParameters = null; }); await frame.locator("#detailCopy").click();
      await inner.waitForFunction(() => typeof window.__copiedParameters === "string"); assert.equal(await inner.evaluate(() => window.__copiedParameters), apiText);
      assert.deepEqual(errors, [], `${name}: browser errors`);
      await page.close(); console.log(`${name}: ComfyUI combined prompt, stages, save/preview branches, sparse overrides, exact copy/download passed`);
    }
    console.log(`Safe fixture screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
