/* Isolated real-parser import checks using safe synthetic display snapshots. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium, webkit } = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const base = process.env.STUDIO_TEST_URL;
if (!base) throw new Error("Set STUDIO_TEST_URL to a fresh isolated WebUI harness.");
const python = process.env.STUDIO_PYTHON || "/home/coder/apps/miniconda3/envs/astrbot/bin/python";
const output = fs.mkdtempSync(path.join(os.tmpdir(), "image-studio-display-snapshots-"));
const fixtureScript = String.raw`
import json,sys
from pathlib import Path
from PIL import Image,ImageDraw,PngImagePlugin
folder=Path(sys.argv[1]);name=sys.argv[2];scenario=sys.argv[3] if len(sys.argv)>3 else "conflict"
snap_a="mountain lake, daylight, a small sailing boat"
snap_b="mountain lake, sunset, two birds above the water"
graph={
 "1":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":"safe-snapshot-landscape.safetensors"}},
 "2":{"class_type":"TextInput","inputs":{"text":"mountain lake"}},
 "3":{"class_type":"CustomTextTransform","inputs":{"text":["2",0],"operation":"runtime_transform"}},
 "4":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":"blur, watermark"}},
 "5":{"class_type":"CLIPTextEncode","inputs":{"clip":["1",1],"text":["3",0]}},
 "6":{"class_type":"EmptyLatentImage","inputs":{"width":640,"height":480,"batch_size":1}},
 "7":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["5",0],"negative":["4",0],"latent_image":["6",0],"seed":41,"steps":24,"cfg":6,"sampler_name":"euler","scheduler":"normal","denoise":1}},
 "8":{"class_type":"VAEDecode","inputs":{"samples":["7",0],"vae":["1",2]}},
 "9":{"class_type":"SaveImage","inputs":{"images":["8",0],"filename_prefix":"safe_display_snapshot"}},
 "90":{"class_type":"easy showAnything","inputs":{"anything":["3",0],"text":"PREVIOUS_RUN_API_DISPLAY"}},
 "91":{"class_type":"easy showAnything","inputs":{"anything":["3",1],"text":"WRONG_OUTPUT_PORT"}},
 "92":{"class_type":"easy showAnything","inputs":{"anything":["2",0],"text":"ALREADY_KNOWN_STATIC_OUTPUT"}},
 "93":{"class_type":"easy showAnything","inputs":{"anything":["3",0],"text":"PREVIOUS_RUN_API_DISPLAY"}},
 "94":{"class_type":"easy showAnything","inputs":{"anything":["3",0],"text":"PREVIOUS_RUN_API_DISPLAY"}}
}
if scenario=="fallback":
 for key in ("90","93","94"): graph[key]["inputs"]["text"]=snap_b
nodes=[];links=[]
for key,node in graph.items():
 inputs=[]
 for field,value in node["inputs"].items():
  if isinstance(value,list):
   link_id=len(links)+1;links.append([link_id,int(value[0]),value[1],int(key),len(inputs),"STRING"])
   inputs.append({"name":field,"type":"STRING","link":link_id})
 widgets=([snap_b] if scenario=="preferred" or key=="90" else [snap_a]) if key in ("90","93","94") and scenario!="fallback" else []
 nodes.append({"id":int(key),"type":node["class_type"],"inputs":inputs,"widgets_values":widgets,"pos":[0,0],"size":[200,100],**({"outputs":[{"name":"text","type":"STRING"}]} if key=="3" else {})})
workflow={"nodes":nodes,"links":links,"groups":[],"version":0.4,"extra":{"test":name}}
metadata=PngImagePlugin.PngInfo();metadata.add_text("prompt",json.dumps(graph));metadata.add_text("workflow",json.dumps(workflow))
image=Image.new("RGB",(640,480),(174,204,210));draw=ImageDraw.Draw(image);draw.rectangle((0,300,640,480),fill=(112,146,136));draw.polygon([(0,320),(255,100),(505,320)],fill=(122,141,148))
target=folder/f"{name}.png";image.save(target,pnginfo=metadata)
print(json.dumps({"file":str(target),"a":snap_a,"b":snap_b}))
`;

const button = (card, candidate, target = "prompt") => card.locator(`[data-candidate-id="${candidate.id}"][data-candidate-target="${target}"]`);
async function dismissNotice(frame) { if (await frame.locator("#appNoticeClose").isVisible()) await frame.locator("#appNoticeClose").click(); }
async function editRange(input, start, end, replacement) {
  await input.evaluate((element, edit) => {
    element.setSelectionRange(edit.start, edit.end);
    element.setRangeText(edit.replacement, edit.start, edit.end, "end");
    element.dispatchEvent(new Event("input", { bubbles: true }));
  }, { start, end, replacement });
}

async function verify(browser, name, width) {
  const fixture = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, name], { encoding: "utf8" }));
  const page = await browser.newPage({ viewport: { width, height: width < 600 ? 844 : 1000 }, hasTouch: width < 600 });
  page.setDefaultTimeout(15000);
  const errors = []; page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(base); const frame = page.frameLocator("#studio");
    await frame.locator("#runtimeStatus").filter({ hasText: "已加载" }).waitFor({ state: "attached" });
    await frame.locator('[data-view="import"]').click();
    const inspected = page.waitForResponse((response) => response.url().includes("/imports/inspect"));
    await frame.locator("#importFiles").setInputFiles(fixture.file);
    const payload = await (await inspected).json(); const parsed = payload.data || payload;
    await frame.locator(".import-card-status").filter({ hasText: "已识别" }).waitFor();
    const snapshots = parsed.normalized.prompt_candidates.filter((candidate) => candidate.status === "display_snapshot");
    assert.equal(snapshots.length, 2, "equal snapshots must merge while conflicting alternatives stay separate");
    const a = snapshots.find((candidate) => candidate.text === fixture.a), b = snapshots.find((candidate) => candidate.text === fixture.b);
    assert.ok(a && b); assert.ok(snapshots.every((candidate) => candidate.source_ref === "3:0" && candidate.conflicting));
    assert.deepEqual(a.observations.map((observation) => observation.node_id).sort(), ["93", "94"]);
    assert.deepEqual(b.observations.map((observation) => observation.node_id), ["90"]);
    assert.ok(snapshots.every((candidate) => candidate.snapshot_kind === "workflow" && candidate.freshness === "unverified" && candidate.observations.every((observation) => observation.source === "workflow")));
    assert.ok(snapshots.every((candidate) => !["91", "92"].includes(candidate.node_id)));
    const card = frame.locator(".import-card"); const prompt = card.locator('[data-import-field="prompt"]'); const negative = card.locator('[data-import-field="negative_prompt"]');
    assert.equal(await prompt.inputValue(), "", "display snapshot must not auto-fill the prompt");
    await card.locator(".import-prompt-candidates > summary").click();
    const candidateText = await card.locator(".import-prompt-candidates").textContent();
    assert.match(candidateText, /关联显示快照/); assert.match(candidateText, /工作流执行回写候选/); assert.match(candidateText, /未验证是否为本次结果/); assert.match(candidateText, /同一输出存在不同快照/);
    assert.doesNotMatch(candidateText, /API 备用显示值|PREVIOUS_RUN_API_DISPLAY|WRONG_OUTPUT_PORT|ALREADY_KNOWN_STATIC_OUTPUT/);
    const prefix = "manual composition note", suffix = "keep this final instruction";
    await prompt.fill(prefix); await button(card, a).click();
    assert.equal(await prompt.inputValue(), `${prefix}\n\n${fixture.a}`);
    assert.equal(await button(card, a).isDisabled(), true, "re-click cannot duplicate a snapshot");
    assert.equal(await button(card, b).textContent(), "改用正向");
    await prompt.fill(`${prefix}\n\n${fixture.a}\n\n${suffix}`); await button(card, b).click();
    assert.equal(await prompt.inputValue(), `${prefix}\n\n${fixture.b}\n\n${suffix}`, "switch replaces only adopted snapshot");
    await button(card, a, "negative_prompt").click();
    assert.equal(await negative.inputValue(), `blur, watermark\n\n${fixture.a}`, "target directions keep independent selections");
    await button(card, b, "negative_prompt").click();
    assert.equal(await negative.inputValue(), `blur, watermark\n\n${fixture.b}`);
    assert.equal(await prompt.inputValue(), `${prefix}\n\n${fixture.b}\n\n${suffix}`);
    await prompt.fill(`extra prefix\n\n${prefix}\n\n${fixture.b}\n\n${suffix}`); await button(card, a).click();
    assert.equal(await prompt.inputValue(), `extra prefix\n\n${prefix}\n\n${fixture.a}\n\n${suffix}`, "edits before the adopted block preserve position tracking");
    const current = await prompt.inputValue(); const blockStart = current.indexOf(fixture.a);
    await editRange(prompt, blockStart + 5, blockStart + 5, " custom");
    const edited = await prompt.inputValue(); await button(card, b).click();
    assert.equal(await prompt.inputValue(), edited, "manually edited snapshot must not be silently replaced or concatenated with a conflicting choice");
    assert.match(await frame.locator("#appNotice").textContent(), /已被手动修改/);
    await dismissNotice(frame);
    await editRange(prompt, blockStart, blockStart + fixture.a.length + " custom".length, "");
    await button(card, b).click();
    assert.ok((await prompt.inputValue()).startsWith(`extra prefix\n\n${prefix}`)); assert.ok((await prompt.inputValue()).includes(suffix));
    assert.equal((await prompt.inputValue()).split(fixture.b).length - 1, 1, "explicit removal allows selecting a new snapshot");
    await prompt.fill(""); await button(card, a).click(); assert.equal(await prompt.inputValue(), fixture.a, "clearing field resets adoption");
    await dismissNotice(frame);
    await card.locator(`[data-prompt-candidate="${b.id}"]`).scrollIntoViewIfNeeded();
    const inner = page.frames().find((item) => item.url().includes("/ui/"));
    const dimensions = await inner.evaluate(() => ({ viewport: document.documentElement.clientWidth, page: document.documentElement.scrollWidth }));
    assert.ok(dimensions.page <= dimensions.viewport + 1, `${name}: horizontal overflow`);
    await page.screenshot({ path: path.join(output, `${name}.png`) });
    for (const scenario of ["preferred", "fallback"]) {
      await dismissNotice(frame); await frame.locator("#cancelImportButton").click();
      const next = JSON.parse(execFileSync(python, ["-c", fixtureScript, output, `${name}-${scenario}`, scenario], { encoding: "utf8" }));
      const response = page.waitForResponse((item) => item.url().includes("/imports/inspect"));
      await frame.locator("#importFiles").setInputFiles(next.file);
      const body = await (await response).json(); const normalized = (body.data || body).normalized;
      await frame.locator(".import-card-status").filter({ hasText: "已识别" }).waitFor();
      const rows = normalized.prompt_candidates, displays = rows.filter((candidate) => candidate.status === "display_snapshot");
      assert.equal(displays.length, 1); assert.equal(displays[0].text, next.b); assert.equal(displays[0].freshness, "unverified");
      assert.equal(displays[0].snapshot_kind, scenario === "preferred" ? "workflow" : "api_fallback");
      assert.ok(!displays[0].conflicting); assert.equal(await prompt.inputValue(), "", "neither preferred workflow nor API fallback auto-fills");
      await card.locator(".import-prompt-candidates > summary").click();
      const visible = await card.locator(".import-prompt-candidates").textContent();
      if (scenario === "preferred") {
        assert.ok(rows.every((candidate) => candidate.id !== "2:text"), "contained upstream text is omitted from candidates");
        assert.deepEqual(displays[0].covered_candidates, [{ id: "2:text", node_id: "2", node_type: "TextInput", field: "text" }]);
        assert.match(visible, /已包含上游 TextInput #2 · text/); assert.match(visible, /工作流执行回写候选/);
        assert.doesNotMatch(visible, /PREVIOUS_RUN_API_DISPLAY|API 备用显示值/);
      } else {
        assert.ok(rows.some((candidate) => candidate.id === "2:text"), "API fallback must not suppress upstream text");
        assert.ok(!displays[0].covered_candidates?.length);
        assert.match(visible, /API 备用显示值，可能来自上一次运行/); assert.match(visible, /未验证是否为本次结果/);
      }
      await card.locator(`[data-prompt-candidate="${displays[0].id}"]`).scrollIntoViewIfNeeded();
      const geometry = await inner.evaluate(() => ({ viewport: document.documentElement.clientWidth, page: document.documentElement.scrollWidth }));
      assert.ok(geometry.page <= geometry.viewport + 1, `${name}-${scenario}: horizontal overflow`);
      await page.screenshot({ path: path.join(output, `${name}-${scenario}.png`) });
    }
    assert.deepEqual(errors, []); console.log(`${name}: workflow priority, API fallback, covered upstream text, conflicts, block replacement and user edits passed`);
  } finally { await page.close(); }
}

(async () => {
  for (const [engine, browserType] of [["chromium", chromium], ["webkit", webkit]]) {
    const browser = await browserType.launch({ headless: true });
    try { for (const width of [390, 1440]) await verify(browser, `${engine}-${width}`, width); }
    finally { await browser.close(); }
  }
  console.log(`Safe snapshot screenshots: ${output}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
