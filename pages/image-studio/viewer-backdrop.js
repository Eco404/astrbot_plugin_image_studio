(() => {
  "use strict";

  // The fullscreen viewer uses one opaque, fixed canvas. Blur is baked into a
  // small thumbnail raster; no live CSS filter or additive compositing layer
  // is rebuilt underneath PhotoSwipe's moving image surface.
  const states = new WeakMap();
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  function themeBaseColor() {
    return getComputedStyle(document.documentElement).getPropertyValue("--page").trim() || "#101312";
  }

  function makeCanvas(width, height) {
    const canvas = document.createElement("canvas");
    canvas.width = width; canvas.height = height;
    return canvas;
  }

  function dimensions() {
    const width = Math.max(1, window.innerWidth), height = Math.max(1, window.innerHeight);
    const scale = Math.min(1, 320 / Math.max(width, height));
    return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)), scale };
  }

  function boxBlur(input, output, width, height, radius, horizontal) {
    const length = horizontal ? width : height, lines = horizontal ? height : width;
    const step = horizontal ? 4 : width * 4, lineStep = horizontal ? width * 4 : 4;
    const divisor = radius * 2 + 1;
    for (let line = 0; line < lines; line++) {
      const start = line * lineStep;
      for (let channel = 0; channel < 3; channel++) {
        let total = 0;
        for (let offset = -radius; offset <= radius; offset++) total += input[start + Math.max(0, Math.min(length - 1, offset)) * step + channel];
        for (let point = 0; point < length; point++) {
          output[start + point * step + channel] = Math.round(total / divisor);
          total += input[start + Math.min(length - 1, point + radius + 1) * step + channel]
            - input[start + Math.max(0, point - radius) * step + channel];
        }
      }
    }
  }

  function rasterize(image, size, baseColor) {
    const canvas = makeCanvas(size.width, size.height);
    const context = canvas.getContext("2d", { alpha: false, willReadFrequently: true });
    if (!context) throw new Error("Canvas rendering unavailable");
    // Bake the original theme-dependent brightness into the opaque pixels.
    // The viewer's separate dark fallback and compositing layers stay intact.
    context.fillStyle = baseColor; context.fillRect(0, 0, size.width, size.height);
    const scale = Math.max(size.width / image.naturalWidth, size.height / image.naturalHeight) * 1.1;
    const width = image.naturalWidth * scale, height = image.naturalHeight * scale;
    context.globalAlpha = .64;
    context.drawImage(image, (size.width - width) / 2, (size.height - height) / 2, width, height);
    context.globalAlpha = 1;
    // Three separable box passes approximate the old 24px blur. The bounded
    // raster also works on WebKit without CanvasRenderingContext2D.filter.
    const pixels = context.getImageData(0, 0, size.width, size.height);
    const scratch = new Uint8ClampedArray(pixels.data);
    const radius = Math.max(1, Math.round(24 * size.scale));
    for (let pass = 0; pass < 3; pass++) {
      boxBlur(pixels.data, scratch, size.width, size.height, radius, true);
      boxBlur(scratch, pixels.data, size.width, size.height, radius, false);
    }
    for (let index = 0; index < pixels.data.length; index += 4) {
      const gray = pixels.data[index] * .2126 + pixels.data[index + 1] * .7152 + pixels.data[index + 2] * .0722;
      for (let channel = 0; channel < 3; channel++) pixels.data[index + channel] = gray + (pixels.data[index + channel] - gray) * .82;
    }
    context.putImageData(pixels, 0, 0);
    return canvas;
  }

  function phase(state) {
    state.canvas.dataset.backdropState = state.paused ? "paused" : state.job ? (state.job.started ? "fading" : "loading") : "idle";
  }

  function draw(state, source, alpha = 1) {
    state.context.globalAlpha = alpha;
    state.context.drawImage(source, 0, 0, state.canvas.width, state.canvas.height);
    state.context.globalAlpha = 1;
  }

  function cancelJob(state) {
    cancelAnimationFrame(state.raf); state.raf = 0;
    const job = state.job;
    state.job = null;
    if (job) { clearTimeout(job.timer); job.cancelDecode?.(); job.resolve(false); }
    delete state.canvas.dataset.pendingSource;
    delete state.canvas.dataset.fadeProgress;
  }

  function paintFrame(state, now) {
    state.raf = 0;
    const job = state.job;
    if (!job || state.disposed || state.paused || !state.canvas.isConnected) return;
    if (!job.image) return;
    if (!job.started) {
      try { job.target = rasterize(job.image, state.size, job.baseColor); }
      catch { cancelJob(state); phase(state); return; }
      job.from = makeCanvas(state.canvas.width, state.canvas.height);
      job.from.getContext("2d", { alpha: false }).drawImage(state.canvas, 0, 0);
      job.started = true; job.lastTime = now; job.elapsed = 0; state.visualDirty = true;
      phase(state);
    }
    job.elapsed += Math.max(0, now - job.lastTime); job.lastTime = now;
    const progress = Math.min(1, job.elapsed / (reduced.matches ? 100 : 280));
    const eased = progress * progress * (3 - 2 * progress);
    // Both rasters are opaque. Drawing the previous frame first means every
    // presented frame has complete coverage, including transparent source art.
    draw(state, job.from); draw(state, job.target, eased);
    state.canvas.dataset.fadeProgress = String(progress);
    if (progress < 1) state.raf = requestAnimationFrame(time => paintFrame(state, time));
    else {
      state.image = job.image; state.source = job.source; state.baseColor = job.baseColor; state.visualDirty = false;
      state.canvas.dataset.previewSource = job.source;
      state.job = null;
      delete state.canvas.dataset.pendingSource;
      delete state.canvas.dataset.fadeProgress;
      phase(state); job.resolve(true);
    }
  }

  function schedule(state) {
    if (state.disposed || state.paused || state.raf || !state.job?.image) return;
    state.job.lastTime = performance.now();
    state.raf = requestAnimationFrame(time => paintFrame(state, time));
  }

  function create(image) {
    if (!image?.complete || !image.naturalWidth) return null;
    try {
      const size = dimensions(), baseColor = themeBaseColor(), raster = rasterize(image, size, baseColor);
      const canvas = makeCanvas(size.width, size.height);
      canvas.className = "image-studio-viewer-backdrop";
      canvas.setAttribute("aria-hidden", "true");
      const context = canvas.getContext("2d", { alpha: false });
      if (!context) return null;
      context.drawImage(raster, 0, 0);
      const state = { canvas, context, size, image, source: image.src, baseColor, job: null, raf: 0, paused: false, disposed: false, resizePending: false, visualDirty: false };
      states.set(canvas, state);
      canvas.dataset.previewSource = image.src; phase(state);
      return canvas;
    } catch { return null; }
  }

  function transition(canvas, source) {
    const state = states.get(canvas);
    if (!state || state.disposed || !canvas.isConnected || !source) return Promise.resolve(false);
    state.paused = false;
    if (state.resizePending) resize(canvas);
    const baseColor = themeBaseColor();
    if (state.job?.source === source && state.job.baseColor === baseColor) { schedule(state); phase(state); return state.job.promise; }
    if (!state.job && state.source === source && state.baseColor === baseColor && !state.visualDirty) { phase(state); return Promise.resolve(true); }
    cancelJob(state);
    // A cancelled partial fade may differ from the committed source; start
    // the next blend from the actual displayed pixels, even when returning.
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    const job = { source, baseColor, promise, resolve, image: null, started: false, lastTime: 0, elapsed: 0, timer: 0, cancelDecode: null };
    state.job = job; canvas.dataset.pendingSource = source; phase(state);
    if (source === state.source) { job.image = state.image; schedule(state); return promise; }
    const image = new Image(); image.className = "image-studio-viewer-backdrop-source"; image.decoding = "async";
    image.src = source;
    const loaded = () => image.complete && image.naturalWidth > 0;
    Promise.race([
      image.decode().then(loaded, () => false),
      new Promise(done => { job.timer = setTimeout(() => done(loaded()), 800); }),
      new Promise(done => { job.cancelDecode = () => done(false); }),
    ]).then(ready => {
      clearTimeout(job.timer);
      if (state.disposed || state.job !== job) return;
      if (!ready) { cancelJob(state); phase(state); return; }
      job.image = image; schedule(state);
    });
    return promise;
  }

  function pause(canvas) {
    const state = states.get(canvas);
    if (!state || state.disposed) return;
    state.paused = true; cancelAnimationFrame(state.raf); state.raf = 0; phase(state);
  }

  function resize(canvas) {
    const state = states.get(canvas);
    if (!state || state.disposed) return;
    if (state.paused) { state.resizePending = true; return; }
    state.resizePending = false;
    const size = dimensions();
    if (size.width === state.size.width && size.height === state.size.height) return;
    const target = state.job?.source;
    const baseColor = themeBaseColor();
    // Rasterize before resizing the visible canvas: setting width/height
    // clears it, so repopulate synchronously in the same task.
    let raster;
    try { raster = rasterize(state.image, size, baseColor); } catch { return; }
    cancelJob(state); state.size = size; state.baseColor = baseColor; state.visualDirty = false;
    canvas.width = size.width; canvas.height = size.height; draw(state, raster); phase(state);
    if (target && target !== state.source) void transition(canvas, target);
  }

  function dispose(canvas) {
    const state = states.get(canvas);
    if (!state) return;
    state.disposed = true; cancelJob(state); states.delete(canvas);
  }

  window.ImageStudioViewerBackdrop = { create, transition, pause, resize, dispose };
})();
