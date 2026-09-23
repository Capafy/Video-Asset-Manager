/*
 * Client-side timeline filmstrips.
 *
 * This module is intentionally standalone: the host may load it after the
 * canonical Studio scripts and call window.VAMFilmstrip.refresh() after each
 * Studio render. It samples real local media frames only; when a source cannot
 * be decoded, the existing poster/empty state remains untouched.
 */
(function (global) {
  "use strict";

  const MAX_ENTRIES = 20;
  const MAX_BYTES = 24 * 1024 * 1024;
  // Keep enough thumbnails to read the motion at normal timeline zoom while
  // remaining bounded for long clips.  The previous 24-frame ceiling made a
  // four-second clip look sparse.
  const FRAME_CAP = 48;
  const FRAME_MIN = 8;
  const LOAD_TIMEOUT = 3500;
  const FAILED_TTL = 30000;
  const cache = new Map();
  const pending = new Map();
  const failed = new Map();
  const queue = [];
  let running = false;
  let clearGeneration = 0;
  let scheduled = false;
  let listenersBound = false;

  const text = (value) => String(value == null ? "" : value);
  const number = (value, fallback = 0) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  };

  function itemFor(id) {
    try {
      if (typeof global.studioAnyItem === "function") return global.studioAnyItem(id);
      if (typeof global.studioItem === "function") return global.studioItem(id);
    } catch (_) { /* optional host APIs */ }
    return null;
  }

  function sourceFor(item) {
    try {
      if (typeof global.studioPreviewUrl === "function") return global.studioPreviewUrl(item);
      const clip = typeof global.studioClipFor === "function" ? global.studioClipFor(item) : null;
      const media = typeof global.studioMediaFor === "function" ? global.studioMediaFor(item) : null;
      const file = clip?.file || media?.file || clip?.proxy || media?.proxy || "";
      return typeof global.furl === "function" ? global.furl(file) : file;
    } catch (_) { return ""; }
  }

  function frameCount(item) {
    const duration = Math.max(.05, number(item?.duration, number(item?.out, 1) - number(item?.in, 0)));
    // A fixed, bounded count lets a generated strip be reused as timeline zoom
    // changes. Short clips use fewer frames without ever repeating a poster.
    if (duration <= .8) return FRAME_MIN;
    if (duration <= 1.8) return 16;
    return Math.min(FRAME_CAP, Math.max(24, Math.ceil(duration * 8)));
  }

  function keyFor(source, item, count) {
    const trimIn = Math.max(0, number(item?.in, 0));
    const trimOut = Math.max(trimIn + .001, number(item?.out, trimIn + number(item?.duration, 1)));
    return [source, trimIn.toFixed(4), trimOut.toFixed(4), count].join("|");
  }

  function touch(entry) {
    entry.lastUsed = Date.now();
    cache.delete(entry.key);
    cache.set(entry.key, entry);
  }

  function release(entry) {
    for (const url of entry.frames || []) {
      if (text(url).startsWith("blob:")) {
        try { URL.revokeObjectURL(url); } catch (_) { /* best effort */ }
      }
    }
    entry.frames = [];
  }

  function trimCache() {
    const totalBytes = () => [...cache.values()].reduce((sum, x) => sum + x.bytes, 0);
    while (cache.size > MAX_ENTRIES || totalBytes() > MAX_BYTES) {
      let victim = null;
      for (const entry of cache.values()) {
        const live = [...(entry.elements || [])].some((node) => node && node.isConnected);
        if (!live) { victim = entry; break; }
      }
      // Never revoke URLs still displayed by a connected timeline item. A
      // temporary cache overshoot is safer than turning visible frames dead.
      if (!victim) break;
      cache.delete(victim.key);
      release(victim);
    }
  }

  function timeoutPromise(ms) {
    return new Promise((_, reject) => setTimeout(() => reject(new Error("media timeout")), ms));
  }

  function loadVideo(source) {
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.crossOrigin = "anonymous";
    video.src = source;
    const loaded = new Promise((resolve, reject) => {
      const done = () => { cleanup(); resolve(video); };
      const fail = () => { cleanup(); reject(new Error("media unavailable")); };
      const cleanup = () => {
        video.removeEventListener("loadedmetadata", done);
        video.removeEventListener("error", fail);
      };
      video.addEventListener("loadedmetadata", done, { once: true });
      video.addEventListener("error", fail, { once: true });
    });
    video.load();
    return Promise.race([loaded, timeoutPromise(LOAD_TIMEOUT)]).catch((error) => {
      try { video.removeAttribute("src"); video.load(); } catch (_) { /* best effort */ }
      throw error;
    });
  }

  function seek(video, at) {
    return new Promise((resolve, reject) => {
      let timer = null;
      const cleanup = () => {
        if (timer) clearTimeout(timer);
        video.removeEventListener("seeked", done);
        video.removeEventListener("error", fail);
      };
      const done = () => { cleanup(); resolve(); };
      const fail = () => { cleanup(); reject(new Error("seek failed")); };
      timer = setTimeout(fail, LOAD_TIMEOUT);
      video.addEventListener("seeked", done, { once: true });
      video.addEventListener("error", fail, { once: true });
      try {
        video.currentTime = Math.max(0, Math.min(number(video.duration, at), at));
      } catch (_) { fail(); }
    });
  }

  function canvasFrame(video) {
    const sourceWidth = Math.max(2, number(video.videoWidth, 320));
    const sourceHeight = Math.max(2, number(video.videoHeight, 180));
    const scale = Math.min(320 / sourceWidth, 180 / sourceHeight, 1);
    const width = Math.max(2, Math.round(sourceWidth * scale));
    const height = Math.max(2, Math.round(sourceHeight * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) throw new Error("canvas unavailable");
    context.drawImage(video, 0, 0, width, height);
    return new Promise((resolve, reject) => canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("frame unavailable")), "image/jpeg", .72));
  }

  async function decode(source, item, count, generation) {
    const video = await loadVideo(source);
    const duration = Math.max(.001, number(video.duration, number(item?.out, 1) - number(item?.in, 0)));
    const start = Math.max(0, Math.min(duration, number(item?.in, 0)));
    const end = Math.max(start + .001, Math.min(duration, number(item?.out, start + number(item?.duration, 1))));
    const frames = [];
    let bytes = 0;
    try {
      for (let index = 0; index < count; index += 1) {
        if (generation !== clearGeneration) throw new Error("cleared");
        const ratio = (index + .5) / count;
        await seek(video, start + (end - start) * ratio);
        const blob = await canvasFrame(video);
        const url = URL.createObjectURL(blob);
        frames.push(url);
        bytes += blob.size || 0;
        // Yield between decodes. This keeps drag/selection responsive and
        // guarantees that only this one hidden video is decoding at a time.
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    } catch (error) {
      for (const url of frames) if (url.startsWith("blob:")) {
        try { URL.revokeObjectURL(url); } catch (_) { /* best effort */ }
      }
      throw error;
    } finally {
      try { video.pause(); video.removeAttribute("src"); video.load(); } catch (_) { /* best effort */ }
    }
    if (!frames.length) throw new Error("no frames");
    return { frames, bytes };
  }

  function apply(element, entry) {
    if (!element?.isConnected || element.dataset.vamFilmstripPending !== entry.key) return;
    let strip = element.querySelector(":scope > .studio-filmstrip");
    if (!strip) {
      strip = document.createElement("span");
      strip.className = "studio-filmstrip has-frames";
      strip.setAttribute("aria-hidden", "true");
      element.appendChild(strip);
    }
    strip.classList.remove("repeated-poster");
    strip.classList.add("has-frames");
    strip.style.backgroundImage = "none";
    strip.replaceChildren(...entry.frames.map((url) => {
      const image = document.createElement("img");
      image.alt = "";
      // Filmstrip frames live inside a horizontally scrolling lane. Browser
      // lazy-loading treats the later thumbnails as offscreen and leaves a
      // black tail until the lane scrolls.
      image.loading = "eager";
      image.decoding = "async";
      image.draggable = false;
      image.src = url;
      return image;
    }));
    element.classList.add("has-filmstrip");
    element.dataset.vamFilmstripKey = entry.key;
    delete element.dataset.vamFilmstripPending;
    (entry.elements || (entry.elements = new Set())).add(element);
  }

  function enqueue(element, item, source, count, generation) {
    const key = keyFor(source, item, count);
    element.dataset.vamFilmstripPending = key;
    const cached = cache.get(key);
    if (cached) { touch(cached); apply(element, cached); return; }
    const failedAt = failed.get(key);
    if (failedAt && Date.now() - failedAt < FAILED_TTL) return;
    if (failedAt) failed.delete(key);
    if (pending.has(key)) { pending.get(key).elements.add(element); return; }
    const job = { key, source, item: { ...item }, count, generation, elements: new Set([element]) };
    pending.set(key, job);
    queue.push(job);
    drain();
  }

  async function drain() {
    if (running) return;
    running = true;
    while (queue.length) {
      const job = queue.shift();
      if (!job) continue;
      try {
        const result = await decode(job.source, job.item, job.count, job.generation);
        if (job.generation !== clearGeneration) {
          for (const url of result.frames) if (url.startsWith("blob:")) URL.revokeObjectURL(url);
        } else {
          const entry = { key: job.key, frames: result.frames, bytes: result.bytes, lastUsed: Date.now(), elements: new Set() };
          const previous = cache.get(job.key);
          if (previous) release(previous);
          cache.set(job.key, entry);
          trimCache();
          for (const element of job.elements) if (element.isConnected) apply(element, entry);
        }
      } catch (_) {
        // Keep the existing poster/empty state. A failed local decode is not
        // a timeline error and must never block editing or generation.
        failed.set(job.key, Date.now());
      } finally { pending.delete(job.key); }
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    running = false;
  }

  function visible(element) {
    const rect = element.getBoundingClientRect();
    return rect.bottom >= -240 && rect.top <= (global.innerHeight || 900) + 240;
  }

  function refresh() {
    if (scheduled) return;
    scheduled = true;
    const run = () => {
      scheduled = false;
      const nodes = [...document.querySelectorAll(".studio-timeline-item[data-timeline-id]")];
      const ordered = nodes.filter(visible);
      for (const element of ordered) {
        const id = element.dataset.timelineId;
        const picked = itemFor(id);
        const track = picked?.track;
        const item = picked?.item;
        if (!item || track?.kind !== "video") continue;
        const source = sourceFor(item);
        if (!source) continue;
        const count = frameCount(item);
        const key = keyFor(source, item, count);
        if (element.dataset.vamFilmstripKey === key) continue;
        enqueue(element, item, source, count, clearGeneration);
      }
    };
    if (typeof global.requestIdleCallback === "function") global.requestIdleCallback(run, { timeout: 500 });
    else setTimeout(run, 0);
  }

  function clear() {
    clearGeneration += 1;
    queue.length = 0;
    pending.clear();
    failed.clear();
    for (const entry of cache.values()) release(entry);
    cache.clear();
  }

  function bindListeners() {
    if (listenersBound) return;
    listenersBound = true;
    const schedule = () => refresh();
    global.addEventListener("scroll", schedule, { passive: true });
    global.addEventListener("resize", schedule, { passive: true });
  }

  global.VAMFilmstrip = { refresh, clear };
  bindListeners();
})(window);
