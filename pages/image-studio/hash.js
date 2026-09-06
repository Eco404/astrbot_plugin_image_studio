(function () {
  "use strict";

  async function fileSHA256(file) {
    if (window.crypto?.subtle) {
      try {
        const digest = await window.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
        return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
      } catch { /* HTTP deployments and sandbox policies may disable Web Crypto. */ }
    }
    if (!window.sha256?.create) throw new Error("图片校验组件未加载，请刷新页面后重试。");
    const hash = window.sha256.create();
    const chunkSize = 512 * 1024;
    for (let offset = 0; offset < file.size; offset += chunkSize) {
      hash.update(await file.slice(offset, offset + chunkSize).arrayBuffer());
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    }
    return hash.hex();
  }

  window.ImageStudioHash = { fileSHA256 };
})();
