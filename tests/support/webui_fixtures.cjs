"use strict";

const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { fixtures } = require("./webui_paths.cjs");

function metadataImage(kind, nonce = "fixture") {
  const webp = kind.endsWith("-webp") || kind === "a1111";
  const extension = webp ? "webp" : "png";
  return {
    name: `synthetic-${kind}.${extension}`,
    mimeType: `image/${extension}`,
    buffer: execFileSync(process.env.STUDIO_PYTHON || "python3", [path.join(fixtures, "image_metadata.py"), kind, nonce], { maxBuffer: 2 * 1024 * 1024 }),
  };
}

module.exports = { metadataImage };
