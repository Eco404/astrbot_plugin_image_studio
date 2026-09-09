/* Safe synthetic text candidates; run only against a fresh isolated harness. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to an isolated WebUI harness.");
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-prompt-candidates-"));
const apiRoot = `${base.replace(/\/$/, "")}/astrbot_plugin_image_studio`;

const fixtureScript = String.raw`
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw, PngImagePlugin
folder=Path(sys.argv[1]); name=sys.argv[2]; model=f"candidate-landscape-{name}.safetensors"
plain="mountain lake with soft clouds\n" + "long-landscape-description-"*14
wildcard="__clear_sky__ {morning|afternoon} reflections"
branch2="branch-two meadow with distant mountain ridges"
graph={
 "1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":model}},
 "2":{"class_type":"TextInput","inputs":{"text":plain}},
 "3":{"class_type":"CustomTextTransform","inputs":{"text":["2",0],"operation":"runtime_transform"}},
 "4":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"blur, watermark"}},
 "5":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":["3",0]}},
 "6":{"class_type":"EmptyLatentImage","inputs":{"width":640,"height":480,"batch_size":1}},
 "7":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["5",0],"negative":["4",0],"latent_image":["6",0],"seed":1152921504606847099,"steps":24,"cfg":6,"sampler_name":"euler","scheduler":"normal","denoise":1}},
 "8":{"class_type":"VAEDecode","inputs":{"samples":["7",0],"vae":["1",2]}},
 "9":{"class_type":"FaceDetailer","inputs":{"image":["8",0],"model":["1",0],"clip":["1",1],"vae":["1",2],"positive":["5",0],"negative":["4",0],"wildcard":wildcard,"steps":12,"cfg":4,"denoise":0.3}},
 "10":{"class_type":"SaveImage","inputs":{"images":["9",0],"filename_prefix":"safe_branch_one"}},
 "90":{"class_type":"easy showAnything","inputs":{"anything":["3",0],"text":"ASSOCIATED_DISPLAY_SNAPSHOT"}},
 "99":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"DISCONNECTED_DEBUG_TEXT_MUST_NOT_BE_LISTED"}}
}
second={
 "16":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":branch2}},
 "17":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"second-branch blur"}},
 "18":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["16",0],"negative":["17",0],"latent_image":["6",0],"seed":123,"steps":16,"cfg":5,"sampler_name":"euler","scheduler":"normal","denoise":1}},
 "19":{"class_type":"VAEDecode","inputs":{"samples":["18",0],"vae":["1",2]}},
 "20":{"class_type":"SaveImage","inputs":{"images":["19",0],"filename_prefix":"safe_branch_two"}}
}
files={}; workflows={}
for kind in ("single", "multi", "group_a", "group_b"):
 data={**graph,**(second if kind=="multi" else {})}
 workflow={"nodes":[{"id":int(key),"type":node["class_type"],"inputs":[],"widgets_values":[],"pos":[int(key)*2,40],"size":[200,100]} for key,node in data.items()],"links":[],"groups":[],"extra":{"test":name,"kind":kind,"run":folder.name},"version":0.4}
 raw_workflow=json.dumps(workflow,indent=2)+"\n";raw_graph=json.dumps(data,indent=1)+"\n"
 workflow_path=folder/f"{name}-{kind}-workflow.json"; workflow_path.write_text(raw_workflow);workflows[kind]=str(workflow_path)
 image=Image.new("RGB",(640,480),(175,206,212));draw=ImageDraw.Draw(image);draw.rectangle((0,300,640,480),fill=(102,148,137));draw.polygon([(0,320),(250,90),(500,320)],fill=(117,143,151));draw.ellipse((440,75,535,130),fill=(231,236,226));draw.line((0,390,640,345),fill=(208,220,197),width=6)
 metadata=PngImagePlugin.PngInfo();metadata.add_text("prompt",raw_graph);metadata.add_text("workflow",raw_workflow)
 target=folder/f"{name}-{kind}.png";image.save(target,pnginfo=metadata);files[kind]=str(target)
print(json.dumps({"files":files,"workflows":workflows,"plain":plain,"wildcard":wildcard,"branch2":branch2,"model":model}))
`;

async function api(page, endpoint) {
  const response = await page.request.get(`${apiRoot}/${endpoint}`); assert.ok(response.ok());
  const body = await response.json(); return body.data || body;
}

async function jsonResponse(response) { const body = await response.json(); return body.data || body; }

async function settle(inner) {
  await inner.evaluate(async () => Promise.all(document.getAnimations().filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => {}))));
}

async function capture(page, inner, name) {
  if (await inner.locator("#appNoticeClose").isVisible()) await inner.locator("#appNoticeClose").click();
  await settle(inner);
  const geometry = await inner.evaluate(() => ({ viewport: document.documentElement.clientWidth, page: document.documentElement.scrollWidth }));
  assert.ok(geometry.page <= geometry.viewport + 1, `${name}: horizontal overflow ${JSON.stringify(geometry)}`);
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

function candidates(scope) { return scope.locator(".import-prompt-candidates"); }
function candidateButton(scope, id, target) { return scope.locator(`[data-candidate-id="${id}"][data-candidate-target="${target}"]`); }

async function expandCandidates(scope) {
  const section = candidates(scope); await section.waitFor();
  if (!await section.evaluate((item) => item.open)) await section.locator(":scope > summary").click();
}

async function chooseOutput(page, scope, value) {
  const source = scope.locator("[data-import-output]");
  const index = await source.evaluate((select, target) => Array.from(select.options).findIndex((option) => option.value === target), value);
  assert.ok(index >= 0, `missing save output ${value}`);
  const response = page.waitForResponse((item) => item.url().includes("/imports/inspect") && String(item.request().postDataJSON()?.output_node_id) === value);
  await source.locator("xpath=..").locator(".studio-select-trigger").click();
  await page.frameLocator("#studio").locator(`.studio-select-menu [data-option-index="${index}"]`).click();
  return await jsonResponse(await response);
}

async function stage(page, frame, file) {
  const inspected = page.waitForResponse((response) => response.url().includes("/imports/inspect"));
  await frame.locator("#importFiles").setInputFiles(file);
  const payload = await jsonResponse(await inspected);
  await frame.locator(".import-card-status").filter({ hasText: "已识别" }).first().waitFor();
  return payload;
}

async function submitSingle(page, frame) {
  const prepare = page.waitForRequest((request) => request.url().includes("/imports/prepare"));
  const uploaded = page.waitForResponse((response) => /\/imports\/group\/[^/]+\/commit/.test(response.url()));
  await frame.locator("#confirmImportButton").click();
  const request = (await prepare).postDataJSON(); const result = await jsonResponse(await uploaded);
  await frame.locator("#importGrid").filter({ hasNot: frame.locator(".import-card") }).waitFor({ state: "attached" });
  return { request, result };
}

async function verifySingle(page, frame, inner, fixture, name) {
  const parsed = await stage(page, frame, fixture.files.single);
  const rows = parsed.normalized.prompt_candidates;
  assert.ok(Array.isArray(rows) && rows.length);
  const text = rows.find((row) => row.id === "2:text"); const wildcard = rows.find((row) => row.id === "9:wildcard");
  assert.ok(text); assert.equal(text.status, "unknown_path"); assert.equal(text.text, fixture.plain); assert.equal(wildcard.status, "template");
  assert.deepEqual(text.output_node_ids, ["10"]); assert.ok(text.stage_ids.includes("7")); assert.ok(text.output_ports.includes(0));
  assert.ok(rows.some((row) => row.node_id === "90" && row.status === "display_snapshot"));
  assert.ok(rows.every((row) => row.node_id !== "99"));
  const card = frame.locator(".import-card").first();
  assert.equal(await candidates(card).evaluate((element) => element.open), false, "candidate section starts collapsed");
  await expandCandidates(card);
  const visible = await candidates(card).textContent(); assert.match(visible, /ASSOCIATED_DISPLAY_SNAPSHOT/); assert.doesNotMatch(visible, /DISCONNECTED_DEBUG/);
  const prompt = card.locator('[data-import-field="prompt"]'); const negative = card.locator('[data-import-field="negative_prompt"]');
  await prompt.fill(""); await candidateButton(card, "2:text", "prompt").click();
  assert.equal(await prompt.inputValue(), fixture.plain);
  assert.equal(await candidateButton(card, "2:text", "prompt").isDisabled(), true);
  assert.match(await candidateButton(card, "2:text", "prompt").textContent(), /已填入/);
  assert.equal(await candidateButton(card, "2:text", "negative_prompt").isDisabled(), false);
  await prompt.fill("manual composition note"); await candidateButton(card, "2:text", "prompt").click();
  assert.match(await prompt.inputValue(), /^manual composition note/); assert.equal((await prompt.inputValue()).split(fixture.plain).length - 1, 1);
  await candidateButton(card, "9:wildcard", "prompt").click();
  const chosen = await prompt.inputValue(); assert.match(chosen, /__clear_sky__/);
  await negative.fill(""); await candidateButton(card, "2:text", "negative_prompt").click(); assert.equal(await negative.inputValue(), fixture.plain);
  await negative.fill("");
  await card.locator('[data-prompt-candidate="2:text"] .prompt-candidate-text > summary').click();
  assert.equal(await card.locator('[data-prompt-candidate="2:text"] pre').textContent(), fixture.plain);
  await capture(page, inner, `${name}-single-candidates`);
  const submitted = await submitSingle(page, frame);
  assert.equal(submitted.request.items[0].overrides.prompt, chosen); assert.equal(submitted.request.items[0].overrides.negative_prompt, "");
  const detail = await api(page, `gallery/detail/${submitted.result.generation_id}?assets=0`);
  assert.equal(detail.original_prompt, chosen); assert.equal(detail.supplemental.overrides.negative_prompt, "");
  return detail.id;
}

async function verifyMultiple(page, frame, inner, fixture, name) {
  let uploadCount = 0; const counter = (request) => { if (request.url().includes("/imports/upload/")) uploadCount++; }; page.on("request", counter);
  const parsed = await stage(page, frame, fixture.files.multi);
  assert.equal(parsed.normalized.requires_output_selection, true);
  assert.equal((parsed.normalized.prompt_candidates || []).length, 0); assert.ok(!parsed.normalized.prompt);
  const card = frame.locator(".import-card").first();
  if (!await frame.locator("#confirmImportButton").isDisabled()) await frame.locator("#confirmImportButton").click();
  await page.waitForTimeout(80); assert.equal(uploadCount, 0, "unselected multiple Save outputs must block uploading");
  const first = await chooseOutput(page, card, "10");
  assert.equal(first.normalized.requires_output_selection, false); assert.equal(String(first.normalized.selected_output_node), "10");
  assert.ok(first.normalized.prompt_candidates.some((item) => item.id === "2:text")); assert.ok(first.normalized.prompt_candidates.every((item) => item.node_id !== "16"));
  await expandCandidates(card);
  const prompt = card.locator('[data-import-field="prompt"]'); await prompt.fill("keep this manual composition");
  const second = await chooseOutput(page, card, "20");
  assert.equal(String(second.normalized.selected_output_node), "20");
  assert.ok(second.normalized.prompt_candidates.some((item) => item.id === "16:text")); assert.ok(second.normalized.prompt_candidates.every((item) => !["2", "9", "90", "99"].includes(item.node_id)));
  assert.equal(await prompt.inputValue(), "keep this manual composition", "output change must not overwrite user-edited text");
  assert.equal(await card.locator('[data-import-field="negative_prompt"]').inputValue(), "second-branch blur", "automatic fields must update for the chosen branch");
  await expandCandidates(card); await candidateButton(card, "16:text", "prompt").click();
  assert.equal((await prompt.inputValue()).split(fixture.branch2).length - 1, 1);
  await prompt.fill("");
  await capture(page, inner, `${name}-multi-output-selected`);
  const submitted = await submitSingle(page, frame); page.off("request", counter);
  assert.equal(submitted.request.items[0].overrides.comfy_output_node, "20"); assert.equal(submitted.request.items[0].overrides.prompt, "");
  const detail = await api(page, `gallery/detail/${submitted.result.generation_id}?assets=0`);
  assert.equal(detail.original_prompt, ""); assert.equal(detail.supplemental.overrides.comfy_output_node, "20");
  assert.equal(String(detail.images[0].metadata.normalized.selected_output_node), "20");
  const parameters = await api(page, `gallery/parameters/${detail.id}?image_id=${detail.images[0].id}&format=studio`);
  const studio = typeof parameters.content === "string" ? JSON.parse(parameters.content) : parameters.content;
  assert.equal(studio.data.prompt, "");
  const workflow = await api(page, `gallery/parameters/${detail.id}?image_id=${detail.images[0].id}&format=workflow`);
  assert.equal(workflow.content, fs.readFileSync(fixture.workflows.multi, "utf8"));
  return detail.id;
}

async function verifyGroup(page, frame, inner, fixture, name) {
  await frame.locator("#importFiles").setInputFiles([fixture.files.group_a, fixture.files.group_b]);
  await frame.locator(".import-card").nth(1).waitFor(); await frame.locator("#confirmImportButton:not(:disabled)").waitFor();
  if (await frame.locator("#appNoticeClose").isVisible()) await frame.locator("#appNoticeClose").click();
  await frame.locator("#importAsGroup").locator("xpath=..").click();
  assert.equal(await frame.locator("#importAsGroup").isChecked(), true);
  const prepared = page.waitForRequest((request) => request.url().includes("/imports/prepare"));
  const committed = page.waitForResponse((response) => /\/imports\/group\/[^/]+\/commit/.test(response.url()));
  await frame.locator("#confirmImportButton").click();
  const body = (await prepared).postDataJSON(); const result = await jsonResponse(await committed);
  assert.equal(body.as_group, true); assert.ok(body.items.every((item) => item.overrides.model === fixture.model));
  await frame.locator("#importGrid").filter({ hasNot: frame.locator(".import-card") }).waitFor({ state: "attached" });
  const detail = await api(page, `gallery/detail/${result.generation_id}?assets=0`); assert.equal(detail.images.length, 2);
  assert.ok(detail.images.every((image) => image.metadata.normalized.prompt_candidates.some((item) => item.id === "2:text")));
  await inner.evaluate(() => window.scrollTo(0, 0)); await frame.locator('[data-view="gallery"]').click();
  await frame.locator("#gallerySearch").fill(fixture.model); await frame.locator("#gallerySearch").press("Tab");
  await frame.locator(`[data-gallery-id="${detail.id}"]`).waitFor(); await frame.locator(`[data-gallery-id="${detail.id}"] .gallery-info`).click();
  await frame.locator("#drawerBody .comfy-workflow-info").waitFor();
  await capture(page, inner, `${name}-group-detail`);
  await frame.locator("#closeDrawer").click();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const width of [320, 390, 1440]) for (const theme of ["light", "dark"]) {
      const name = `${width}-${theme}`;
      const fixture = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, name], { encoding: "utf8" }));
      const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
      page.setDefaultTimeout(12000); const errors = []; page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(base); const frame = page.frameLocator("#studio"); await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
      const inner = page.frames().find((item) => item.url().includes("/ui/")); await inner.evaluate(async (value) => { await window.ImageStudioAppearance?.ready; window.ImageStudioAppearance.set({ preference: value }); }, theme);
      await frame.locator('[data-view="import"]').click();
      await verifySingle(page, frame, inner, fixture, name);
      await verifyMultiple(page, frame, inner, fixture, name);
      await verifyGroup(page, frame, inner, fixture, name);
      assert.deepEqual(errors, [], `${name}: browser errors`);
      await page.close(); console.log(`${name}: branch candidates, append deduplication, manual clears, selected outputs, exact workflow, grouped import passed`);
    }
    console.log(`Safe candidate screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
