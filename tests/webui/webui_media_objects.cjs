/* Exercise real Worker/Blob ownership and the yielding UI-thread fallback. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const playwright = require(process.env.STUDIO_PLAYWRIGHT || "playwright");
const script = fs.readFileSync(require("../support/webui_paths.cjs").pagePath("media-objects.js"), "utf8");
const engines = process.env.STUDIO_BROWSER ? [process.env.STUDIO_BROWSER] : ["chromium", "webkit"];

async function fixture(page, mode) {
  await page.setContent('<iframe sandbox="allow-scripts"></iframe>');
  const frame = page.frames().find(item => item !== page.mainFrame());
  await frame.evaluate(mode => {
    window.stats = { created: [], revoked: [], workers: 0, posts: 0, terminated: 0, pieces: [], yielded: 0 };
    const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = blob => {
      const url = create(blob);
      stats.created.push({ url, type: blob.type });
      return url;
    };
    URL.revokeObjectURL = url => { stats.revoked.push(url); revoke(url); };
    const nativeAtob = window.atob.bind(window), nativeTimeout = window.setTimeout.bind(window);
    window.atob = text => { stats.pieces.push(text.length); return nativeAtob(text); };
    window.setTimeout = (callback, delay, ...args) => {
      if (delay === 0) stats.yielded++;
      return nativeTimeout(callback, delay, ...args);
    };
    const NativeWorker = window.Worker;
    if (mode === "fallback") window.Worker = undefined;
    else if (mode === "denied") window.Worker = class { constructor() { throw new DOMException("Worker forbidden", "SecurityError"); } };
    else window.Worker = class extends NativeWorker {
      constructor(...args) { super(...args); stats.workers++; }
      postMessage(...args) {
        stats.posts++;
        if (mode === "crashed") {
          // Fail after construction/readiness, while source data is in flight.
          nativeTimeout(() => this.dispatchEvent(new ErrorEvent("error", { cancelable: true })), 0);
          return;
        }
        return super.postMessage(...args);
      }
      terminate() { stats.terminated++; return super.terminate(); }
    };
    window.sourceBytes = Uint8Array.from({ length: 1024 * 1024 + 7 }, (_, index) => (index * 31 + 19) % 256);
    let binary = "";
    for (let index = 0; index < sourceBytes.length; index += 8192) binary += String.fromCharCode(...sourceBytes.subarray(index, index + 8192));
    window.input = `data:image/png;base64,${btoa(binary)}`;
    window.second = "data:image/webp;base64,AQIDBAU=";
    window.read = async url => {
      const response = await fetch(url), blob = await response.blob();
      return { type: blob.type, bytes: new Uint8Array(await blob.arrayBuffer()) };
    };
  }, mode);
  await frame.addScriptTag({ content: script });
  return frame;
}

async function verifyConversion(frame, mode) {
  const state = await frame.evaluate(async () => {
    const scope = ImageStudioMediaObjects.createScope();
    const first = scope.source("picture", input), duplicate = scope.source("picture", input);
    const samePromise = first === duplicate;
    const url = await first;
    const result = await read(url);
    const equal = result.bytes.length === sourceBytes.length && result.bytes.every((byte, index) => byte === sourceBytes[index]);
    const beforeDrop = stats.revoked.includes(url);
    scope.drop("picture"); scope.drop("picture"); scope.dispose(); scope.dispose();
    return { samePromise, url, equal, type: result.type, beforeDrop, ...stats, origin: location.origin };
  });
  assert.equal(state.samePromise, true, "in-flight duplicate callers share a conversion");
  assert.match(state.url, /^blob:/); assert.equal(state.equal, true); assert.equal(state.type, "image/png");
  assert.equal(state.origin, "null", "worker fixture models the bridge's opaque iframe");
  assert.equal(state.beforeDrop, false);
  assert.equal(state.revoked.filter(url => url === state.url).length, 1, "each owned image URL is revoked once");
  assert.equal(state.created.length, state.revoked.length, "dispose releases the worker script and image URLs");
  if (mode === "worker") {
    assert.equal(state.workers, 1); assert.equal(state.posts, 1); assert.equal(state.terminated, 1);
    assert.equal(state.pieces.length, 0, "large base64 is never decoded on the UI thread with a worker");
  } else {
    assert.ok(state.pieces.length > 16);
    assert.ok(Math.max(...state.pieces) <= 65536, "fallback never decodes the complete large image at once");
    assert.ok(state.yielded >= state.pieces.length, "each fallback decoding piece yields to input events");
  }
}

async function verifyLifecycle(frame) {
  const state = await frame.evaluate(async () => {
    stats.created.length = stats.revoked.length = 0;
    const scope = ImageStudioMediaObjects.createScope();
    const discarded = scope.source("dropped", input); scope.drop("dropped");
    const old = scope.source("replaced", input), latest = scope.source("replaced", second);
    const url = await latest;
    const result = await read(url);
    const disposed = scope.source("pending", input); scope.dispose();
    const ignored = await scope.source("after-dispose", input);
    await new Promise(resolve => setTimeout(resolve, 25));
    return {
      discarded: await discarded, old: await old, disposed: await disposed, ignored,
      bytes: Array.from(result.bytes), mime: result.type,
      created: stats.created, revoked: stats.revoked,
    };
  });
  assert.equal(state.discarded, ""); assert.equal(state.old, ""); assert.equal(state.disposed, ""); assert.equal(state.ignored, "");
  assert.deepEqual(state.bytes, [1, 2, 3, 4, 5]); assert.equal(state.mime, "image/webp");
  assert.equal(state.created.filter(item => item.type.startsWith("image/")).length, 1, "stale jobs never allocate image URLs");
  for (const item of state.created) assert.equal(state.revoked.filter(url => item.url === url).length, 1);
}

async function verifyReplacementAndValidation(frame) {
  const state = await frame.evaluate(async () => {
    const scope = ImageStudioMediaObjects.createScope();
    const original = await scope.source("same", second);
    const replacement = scope.source("same", "data:image/jpeg;base64,AQID");
    const aliveWhileReplacing = !stats.revoked.includes(original);
    const next = await replacement;
    const firstRevoked = stats.revoked.includes(original);
    const nextBytes = await read(next);
    const invalid = await Promise.all([
      "data:image/svg+xml;base64,PHN2Zz4=", "https://example.org/picture.png",
      "data:text/html;base64,aGk=", "data:image/png;base64,", "data:image/png;base64,!invalid",
    ].map((value, index) => scope.source(`invalid-${index}`, value)));
    const external = URL.createObjectURL(new Blob([new Uint8Array([9])], { type: "image/gif" }));
    const passed = await scope.source("external", external);
    scope.drop("external"); scope.dispose();
    const externalRevoked = stats.revoked.includes(external), externalBytes = await read(external);
    URL.revokeObjectURL(external);
    return { aliveWhileReplacing, firstRevoked, nextBytes: Array.from(nextBytes.bytes), mime: nextBytes.type, invalid, external, passed, externalRevoked, externalBytes: Array.from(externalBytes.bytes) };
  });
  assert.equal(state.aliveWhileReplacing, true, "the previous source remains valid until replacement is ready");
  assert.equal(state.firstRevoked, true); assert.deepEqual(state.nextBytes, [1, 2, 3]); assert.equal(state.mime, "image/jpeg");
  assert.deepEqual(state.invalid, ["", "", "", "", ""]);
  assert.equal(state.passed, state.external); assert.equal(state.externalRevoked, false); assert.deepEqual(state.externalBytes, [9]);
}

(async () => {
  for (const engine of engines) {
    const browser = await playwright[engine].launch({ headless: true });
    try {
      for (const mode of ["worker", "fallback", "denied", "crashed"]) {
        const page = await browser.newPage();
        const frame = await fixture(page, mode);
        await verifyConversion(frame, mode);
        await verifyLifecycle(frame);
        await verifyReplacementAndValidation(frame);
        await page.close();
        console.log(`${engine}: media object ${mode} conversion, duplicate reuse, stale disposal and URL ownership passed`);
      }
    } finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
