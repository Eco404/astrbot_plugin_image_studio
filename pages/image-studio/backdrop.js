(() => {
  "use strict";

  const states = new WeakMap();
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function sourceKey(source) {
    try { return new URL(source, document.baseURI).href; }
    catch { return source; }
  }

  function updatePhase(state) {
    state.image.dataset.backdropState = state.paused ? "paused" : state.pending
      ? (state.pending.fading ? "fading" : "loading")
      : state.fade ? "fading" : "idle";
  }

  function cancelPending(state) {
    const request = state.pending;
    if (!request) return;
    state.pending = null;
    request.cancel();
    request.resolve(false);
    // A decoded candidate may already be the visible cover during a fade.
    if (!request.candidate?.isConnected) request.candidate?.removeAttribute("src");
    updatePhase(state);
  }

  function restoreProperty(style, name, previous) {
    if (previous.value) style.setProperty(name, previous.value, previous.priority);
    else style.removeProperty(name);
  }

  function setOpacity(image, value) {
    const opacity = Number(value ?? .62);
    image.parentElement.style.opacity = String(Number.isFinite(opacity) ? Math.max(0, Math.min(1, opacity)) : .62);
  }

  function fadeTo(state, candidate, previousClass) {
    const image = state.image;
    const oldSource = image.getAttribute("src");
    const hasPrevious = oldSource && image.complete && image.naturalWidth > 0;
    const additive = window.CSS?.supports("mix-blend-mode", "plus-lighter");
    const properties = Object.fromEntries(["animation", "transition"].map((name) => [
      name, { value: image.style.getPropertyValue(name), priority: image.style.getPropertyPriority(name) },
    ]));

    // CSS motion overrides must not shorten this decode-gated, opacity-only transition.
    image.style.setProperty("animation", "none", "important");
    image.style.setProperty("transition", "none", "important");
    candidate.classList.add(...previousClass.split(/\s+/).filter(Boolean));
    candidate.setAttribute("aria-hidden", "true");
    candidate.style.setProperty("animation", "none", "important");
    candidate.style.setProperty("transition", "none", "important");
    candidate.style.opacity = hasPrevious ? "0" : "1";
    // Add premultiplied colors within the isolated background group. Unlike
    // source-over crossfades this preserves coverage, including alpha images.
    if (additive) candidate.style.mixBlendMode = "plus-lighter";
    image.after(candidate);

    let resolve;
    let finished = false;
    let committing = false;
    let commitPending = false;
    let selected = false;
    let endTimer = 0;
    const duration = reducedMotion.matches ? 140 : 280;
    const animations = [];
    const cleanup = (success) => {
      if (finished) return;
      finished = true;
      window.clearTimeout(endTimer);
      image.style.opacity = "1";
      for (const animation of animations) animation.cancel();
      candidate.remove();
      candidate.removeAttribute("src");
      restoreProperty(image.style, "animation", properties.animation);
      restoreProperty(image.style, "transition", properties.transition);
      if (state.fade === fade) state.fade = null;
      updatePhase(state);
      resolve(success);
    };
    async function commit() {
      if (finished || committing) return;
      if (state.paused) { commitPending = true; return; }
      window.clearTimeout(endTimer);
      commitPending = false;
      committing = true;
      // Keep the already decoded, mounted candidate visible until the stable
      // image has selected and painted the same resource. Never swap both srcs.
      candidate.style.opacity = "1";
      image.style.opacity = "0";
      if (image.src !== candidate.src) image.src = candidate.src;
      try {
        // Some engines leave a second decode pending even though this exact
        // resource is already decoded on the mounted candidate. Bound that
        // wait; a fully loaded matching image can then complete the handoff.
        if (!selected) {
          let decodeTimer;
          const loaded = () => image.complete && image.naturalWidth > 0 && sourceKey(image.currentSrc || image.src) === sourceKey(candidate.src);
          selected = await Promise.race([
            image.decode().then(() => true, () => false),
            new Promise(done => { decodeTimer = window.setTimeout(() => done(loaded()), 800); }),
          ]).finally(() => window.clearTimeout(decodeTimer));
          if (!selected) throw new Error("Background image did not become ready");
        }
        if (finished) return;
        await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
        if (finished) return;
        if (state.paused) { committing = false; commitPending = true; return; }
        cleanup(true);
      } catch {
        if (finished) return;
        if (oldSource) image.src = oldSource;
        else image.removeAttribute("src");
        cleanup(false);
      }
    }
    function concludeFade() {
      if (finished || committing) return;
      if (state.paused) { commitPending = true; return; }
      // A paused animation at its end need not resolve its finished promise.
      // Explicitly settle that boundary before handing off the painted layer.
      for (const animation of animations) {
        try { animation.finish(); } catch { animation.cancel(); }
      }
      void commit();
    }
    function watchEnd() {
      window.clearTimeout(endTimer);
      if (finished || committing || state.paused) return;
      const remaining = Math.max(0, ...animations.map(animation => duration - Number(animation.currentTime || 0)));
      // Only count time while running. A missed WebKit finish notification
      // must not leave all later background requests waiting on this fade.
      endTimer = window.setTimeout(concludeFade, remaining + 100);
    }
    const fade = {
      key: sourceKey(candidate.src),
      promise: new Promise((done) => { resolve = done; }),
      pause() {
        window.clearTimeout(endTimer);
        for (const animation of animations) {
          const position = animation.currentTime;
          if (position !== null && position >= duration) continue;
          animation.pause();
          if (position !== null) animation.currentTime = position;
        }
      },
      resume() {
        if (finished) return;
        if (commitPending || animations.every(animation => animation.currentTime !== null && animation.currentTime >= duration)) {
          concludeFade();
          return;
        }
        if (!finished) for (const animation of animations) {
          // The decoded-image handoff may still be pending after the fade ends.
          // Playing an animation at its end would rewind it and flash again.
          if (animation.currentTime < animation.effect.getTiming().duration) animation.play();
        }
        watchEnd();
      },
      cancel() { cleanup(false); },
    };
    state.fade = fade;
    if (!hasPrevious || typeof candidate.animate !== "function") {
      void commit();
      return fade.promise;
    }
    try {
      const options = {
        duration,
        easing: "ease-in-out",
        fill: "both",
      };
      animations.push(candidate.animate([{ opacity: 0 }, { opacity: 1 }], options));
      if (additive) animations.push(image.animate([{ opacity: 1 }, { opacity: 0 }], options));
      // Older engines without additive blending retain the old opaque layer
      // underneath instead of fading it out and exposing the page background.
      Promise.all(animations.map((animation) => animation.finished)).then(concludeFade, concludeFade);
      watchEnd();
    } catch {
      for (const animation of animations) animation.finished.catch(() => {});
      for (const animation of animations) animation.cancel();
      void commit();
    }
    return fade.promise;
  }

  function makeState(image) {
    const state = { image, pending: null, fade: null, observer: null, disposed: false, paused: false };
    state.observer = new MutationObserver(() => {
      if (!image.isConnected) dispose(image);
    });
    state.observer.observe(document.documentElement, { childList: true, subtree: true });
    states.set(image, state);
    return state;
  }

  function transition(image, source, options = {}) {
    if (!(image instanceof HTMLImageElement)) return Promise.resolve(false);
    if (!options || typeof options !== "object") options = {};
    const state = states.get(image) || (image.isConnected ? makeState(image) : null);
    if (!state || state.disposed) return Promise.resolve(false);
    if (state.paused) {
      state.paused = false;
      state.fade?.resume();
      updatePhase(state);
    }
    const nextSource = typeof source === "string" ? source.trim() : "";
    if (!nextSource) {
      cancelPending(state);
      return Promise.resolve(false);
    }
    const key = sourceKey(nextSource);
    if (state.pending?.key === key) return state.pending.promise;
    cancelPending(state);

    let resolve;
    let cancel;
    const cancelled = new Promise((done) => { cancel = () => done(false); });
    const request = {
      key, candidate: null, fading: false, cancel,
      promise: new Promise((done) => { resolve = done; }),
      resolve: (result) => resolve(result),
    };
    state.pending = request;
    updatePhase(state);
    const current = () => !state.disposed && image.isConnected && state.pending === request;
    const opacityValue = Number(options.opacity ?? 0.62);
    const opacity = Number.isFinite(opacityValue) ? Math.max(0, Math.min(1, opacityValue)) : 0.62;
    setOpacity(image, opacity);
    if (!state.fade) image.style.opacity = "1";
    const previousClass = typeof options.previousClass === "string" && options.previousClass.trim()
      ? options.previousClass.trim() : "detail-backdrop-previous";

    async function apply() {
      if (state.fade?.key === key) {
        const painted = await Promise.race([state.fade.promise, cancelled]);
        return painted && current();
      }
      if (image.getAttribute("src") && sourceKey(image.getAttribute("src")) === key && image.complete && image.naturalWidth > 0) {
        if (state.fade) await Promise.race([state.fade.promise, cancelled]);
        return current();
      }
      const candidate = new Image();
      candidate.className = image.className;
      candidate.decoding = "async";
      if (image.crossOrigin) candidate.crossOrigin = image.crossOrigin;
      if (image.referrerPolicy) candidate.referrerPolicy = image.referrerPolicy;
      request.candidate = candidate;
      const loaded = new Promise((done) => {
        candidate.onload = () => done(candidate.naturalWidth > 0);
        candidate.onerror = () => done(false);
      });
      candidate.src = nextSource;
      let decoding;
      try {
        decoding = typeof candidate.decode === "function"
          ? candidate.decode().then(() => candidate.naturalWidth > 0, () => false)
          : loaded;
      } catch {
        decoding = Promise.resolve(false);
      }
      const ready = await Promise.race([decoding, cancelled]);
      candidate.onload = null;
      candidate.onerror = null;
      if (!ready || !current()) return false;

      // Finish the current visual blend before starting another; never stack stale layers.
      if (state.fade) await Promise.race([state.fade.promise, cancelled]);
      if (!current()) return false;
      request.fading = true;
      updatePhase(state);
      const painted = await fadeTo(state, candidate, previousClass);
      return painted && current();
    }

    apply().then((result) => {
      if (state.pending === request) {
        state.pending = null;
        updatePhase(state);
      }
      request.resolve(result);
    }, () => {
      if (state.pending === request) {
        state.pending = null;
        updatePhase(state);
      }
      request.resolve(false);
    });
    return request.promise;
  }

  function pause(image) {
    const state = states.get(image);
    if (!state || state.disposed) return;
    cancelPending(state);
    state.paused = true;
    state.fade?.pause();
    updatePhase(state);
  }

  function dispose(image) {
    const state = states.get(image);
    if (!state) return;
    state.disposed = true;
    cancelPending(state);
    state.fade?.cancel();
    state.observer?.disconnect();
    image.dataset.backdropState = "idle";
    states.delete(image);
  }

  function seed(image, source, options = {}) {
    if (!(image instanceof HTMLImageElement) || !image.isConnected || !image.complete || !image.naturalWidth || !source || sourceKey(image.src) !== sourceKey(source)) return false;
    dispose(image);
    setOpacity(image, options.opacity);
    image.style.opacity = "1";
    const state = makeState(image);
    updatePhase(state);
    return true;
  }

  window.ImageStudioBackdrop = { transition, pause, dispose, seed };
})();
