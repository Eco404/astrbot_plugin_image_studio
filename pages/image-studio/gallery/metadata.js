(function () {
  "use strict";
  const { own } = window.ImageStudioPresentation;

  function decodeComment(value) {
    if (typeof value === "string") return value.replace(/\0+$/, "");
    if (!Array.isArray(value) && !ArrayBuffer.isView(value)) return "";
    if (Array.isArray(value) && value.every((item) => typeof item === "string")) return value.join("").replace(/\0+$/, "");
    const bytes = value instanceof Uint8Array ? value : Uint8Array.from(value);
    const signature = String.fromCharCode(...bytes.slice(0, 8));
    const payload = /^(UNICODE\0|ASCII\0\0\0|JIS\0\0\0\0\0)/.test(signature) ? bytes.slice(8) : bytes;
    if (signature.startsWith("UNICODE")) {
      const sample = payload.slice(0, 128);
      let evenZeros = 0, oddZeros = 0;
      sample.forEach((byte, index) => { if (!byte) index % 2 ? oddZeros++ : evenZeros++; });
      const littleEndian = payload[0] === 255 && payload[1] === 254 || !(payload[0] === 254 && payload[1] === 255) && oddZeros > evenZeros;
      return new TextDecoder(littleEndian ? "utf-16le" : "utf-16be").decode(payload).replace(/\0+$/, "");
    }
    return new TextDecoder(signature.startsWith("JIS") ? "shift_jis" : "utf-8").decode(payload).replace(/\0+$/, "");
  }

  async function extractMetadata(file) {
    if (!window.ExifReader) throw new Error("图片参数读取组件未加载，请刷新页面后重试。");
    const tags = await window.ExifReader.load(await file.arrayBuffer(), { expanded: true, async: true });
    const raw = {};
    // Keep textual JSON untouched so large ComfyUI seeds survive browser parsing.
    for (const [name, tag] of Object.entries(tags.pngText || {})) {
      if (typeof tag.value === "string") raw[name] = tag.value;
    }
    for (const name of ["Software", "ImageDescription", "Make", "Model", "Artist", "Copyright", "UserComment", "DateTime", "DateTimeOriginal", "DateTimeDigitized", "OffsetTime", "OffsetTimeOriginal", "OffsetTimeDigitized", "SubSecTime", "SubSecTimeOriginal", "SubSecTimeDigitized"]) {
      const tag = tags.exif?.[name];
      if (!tag) continue;
      if (name === "UserComment") raw[name] = decodeComment(tag.value);
      else raw[name] = typeof tag.value === "string" ? tag.value : Array.isArray(tag.value) ? tag.value.join("") : tag.description;
    }
    for (const [name, tag] of Object.entries(tags.xmp || {})) {
      if (["parameters", "prompt", "workflow", "Description", "Comment", "Software"].includes(name) && !own(raw, name)) raw[name] = typeof tag.value === "string" ? tag.value : tag.description;
    }
    return raw;
  }

  window.ImageStudioMetadata = { extractMetadata, decodeComment };
})();
