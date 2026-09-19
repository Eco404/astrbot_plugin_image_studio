const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createHash, randomBytes, webcrypto } = require("node:crypto");

const root = require("../support/webui_paths.cjs").frontend;
const library = fs.readFileSync(path.join(root, "vendor/js-sha256/sha256.min.js"), "utf8");
const wrapper = fs.readFileSync(path.join(root, "hash.js"), "utf8");

function context(crypto) {
  let yielded = 0;
  const scope = { crypto, ArrayBuffer, Uint8Array, setTimeout: (callback) => { yielded++; return setTimeout(callback, 0); } };
  scope.window = scope;
  vm.createContext(scope);
  vm.runInContext(library, scope);
  vm.runInContext(wrapper, scope);
  return { hash: scope.ImageStudioHash.fileSHA256, yielded: () => yielded };
}

(async () => {
  const vectors = [Buffer.alloc(0), Buffer.from("abc"), Buffer.from("图片元数据与文件内容"), randomBytes(1024 * 1024 + 7)];
  const native = context(webcrypto);
  const fallback = context(undefined);
  const denied = context({ subtle: { digest: async () => { throw new Error("blocked"); } } });
  for (const bytes of vectors) {
    const expected = createHash("sha256").update(bytes).digest("hex");
    const file = new Blob([bytes]);
    for (const engine of [native, fallback, denied]) assert.equal(await engine.hash(file), expected);
  }
  const bytes = vectors[3];
  assert.equal(await fallback.hash(new File([bytes], "renamed.png")), await fallback.hash(new File([bytes], "original.webp")));
  assert.ok(fallback.yielded() >= 3, "fallback must yield between chunks");
  assert.equal(native.yielded(), 0);
  const broken = { size: 5, arrayBuffer: async () => { throw new Error("file unreadable"); }, slice() { return this; } };
  await assert.rejects(() => native.hash(broken), /file unreadable/);
  console.log("SHA-256 native, sandbox fallback, rejected Web Crypto, chunking, filenames and read failures passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
