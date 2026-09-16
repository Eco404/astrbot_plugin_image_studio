(() => {
  "use strict";

  const imageHeader = /^data:(image\/(?:png|jpeg|webp|gif));base64,/i;
  const chunkSize = 64 * 1024;
  const yieldTask = () => new Promise(resolve => setTimeout(resolve, 0));

  function workerMain() {
    self.onmessage = event => {
      const { id, input, headerLength, mime } = event.data;
      try {
        const binary = atob(input.slice(headerLength));
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
        self.postMessage({ id, blob: new Blob([bytes], { type: mime }) });
      } catch {
        self.postMessage({ id, failed: true });
      }
    };
    self.postMessage({ ready: true });
  }

  // A scope owns only URLs it creates. Use one per viewer, with keys including
  // the media revision/quality. Callers retain returned URLs, not base64 strings.
  function createScope() {
    const entries = new Map(), jobs = new Map();
    let nextId = 0, disposed = false, worker = null, workerUrl = "";
    let workerReady = false, workerUnavailable = false, readyTimer = 0;

    function revoke(url) {
      if (url) URL.revokeObjectURL(url);
    }

    function active(entry) {
      return !disposed && !entry.settled && entries.get(entry.key) === entry;
    }

    function settle(entry, url = "") {
      if (entry.settled) return;
      entry.settled = true;
      jobs.delete(entry.id);
      entry.input = "";
      const resolve = entry.resolve;
      entry.resolve = null;
      resolve(url);
    }

    function finish(entry, blob) {
      if (!active(entry)) { settle(entry); return; }
      let url = "";
      try { if (blob) url = URL.createObjectURL(blob); }
      catch { /* A failed conversion leaves no unusable DOM source. */ }
      if (url) {
        revoke(entry.url);
        entry.url = url;
      } else {
        revoke(entry.url);
        entries.delete(entry.key);
      }
      settle(entry, url);
    }

    async function fallback(entry) {
      if (entry.fallback || !active(entry)) return;
      entry.fallback = true;
      const parts = [];
      try {
        // Decode aligned pieces rather than atob(theEntireImage) on the UI
        // thread. Yield before every piece so a new swipe can take priority.
        for (let offset = entry.headerLength; offset < entry.input.length; offset += chunkSize) {
          await yieldTask();
          if (!active(entry)) return;
          const binary = atob(entry.input.slice(offset, offset + chunkSize));
          const bytes = new Uint8Array(binary.length);
          for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
          parts.push(bytes);
        }
        if (active(entry)) finish(entry, new Blob(parts, { type: entry.mime }));
      } catch { finish(entry, null); }
    }

    function stopWorker() {
      clearTimeout(readyTimer); readyTimer = 0;
      worker?.terminate(); worker = null; workerReady = false;
      revoke(workerUrl); workerUrl = "";
    }

    function workerFailed() {
      workerUnavailable = true;
      stopWorker();
      for (const entry of jobs.values()) fallback(entry);
    }

    function dispatch(entry) {
      if (!active(entry) || entry.sent || entry.fallback || !workerReady) return;
      entry.sent = true;
      try {
        worker.postMessage({ id: entry.id, input: entry.input, headerLength: entry.headerLength, mime: entry.mime });
      } catch { workerFailed(); }
    }

    function startWorker() {
      if (worker || workerUnavailable || disposed) return;
      if (typeof Worker !== "function") { workerFailed(); return; }
      try {
        // A Blob script also works in the page bridge's opaque iframe origin;
        // fetching a worker from a relative HTTP URL would be cross-origin.
        workerUrl = URL.createObjectURL(new Blob([`(${workerMain.toString()})();`], { type: "text/javascript" }));
        worker = new Worker(workerUrl);
        worker.onmessage = event => {
          if (disposed || !worker) return;
          if (event.data?.ready) {
            clearTimeout(readyTimer); readyTimer = 0;
            revoke(workerUrl); workerUrl = "";
            workerReady = true;
            for (const entry of jobs.values()) dispatch(entry);
            return;
          }
          const entry = jobs.get(event.data?.id);
          if (entry) finish(entry, event.data.blob instanceof Blob ? event.data.blob : null);
        };
        worker.onerror = event => { event.preventDefault(); workerFailed(); };
        worker.onmessageerror = workerFailed;
        readyTimer = setTimeout(workerFailed, 4000);
      } catch { workerFailed(); }
    }

    function drop(key) {
      const entry = entries.get(key);
      if (!entry) return;
      entries.delete(key);
      revoke(entry.url);
      entry.url = "";
      settle(entry);
    }

    function source(key, input) {
      if (disposed || typeof input !== "string") return Promise.resolve("");
      // Already-owned sources can pass through without being adopted/revoked.
      if (input.startsWith("blob:")) return Promise.resolve(input);
      const header = imageHeader.exec(input);
      if (!header || input.length === header[0].length) return Promise.resolve("");
      const previous = entries.get(key);
      if (previous && !previous.settled && previous.input === input) return previous.promise;
      const oldUrl = previous?.url || "";
      if (previous) settle(previous);
      let resolve;
      const promise = new Promise(done => { resolve = done; });
      const entry = { key, input, id: ++nextId, headerLength: header[0].length, mime: header[1].toLowerCase(), url: oldUrl, promise, resolve, settled: false, fallback: false, sent: false };
      entries.set(key, entry); jobs.set(entry.id, entry);
      startWorker();
      if (workerUnavailable) fallback(entry);
      else dispatch(entry);
      return promise;
    }

    function dispose() {
      if (disposed) return;
      disposed = true;
      stopWorker();
      for (const key of entries.keys()) drop(key);
    }

    return { source, drop, dispose };
  }

  window.ImageStudioMediaObjects = Object.freeze({ createScope });
})();
