/*
 * Optional Studio workspace affordances.
 *
 * This module only adds the small, ZIP-inspired workspace layer around the
 * canonical Studio.  It deliberately calls the existing timeline functions
 * and API helpers; it does not create a second state store, renderer, or
 * persistence path.  The host calls workspaceStudioEnhance() after
 * renderStudio() has bound its native controls.
 */
(function (global) {
  "use strict";

  const MODES = [
    ["media", "Media"],
    ["text", "Text"],
    ["audio", "Audio"],
    ["transitions", "Transitions"],
  ];
  const TEXT_PRESETS = {
    subtitle: { kind: "subtitle", label: "Dialogue subtitle", sample: "Clear, simple captions", content: "Your subtitle here", style: { font: "Inter", fontSize: 48, position: "bottom", color: "#ffffff", outlineColor: "#111111", outlineWidth: 3, align: "center" } },
    title: { kind: "title", label: "Editorial title", sample: "Every story matters", content: "Your story starts here", style: { font: "Playfair Display", fontSize: 96, position: "center", color: "#ffffff", outlineWidth: 0, align: "center" } },
    pop: { kind: "title", label: "Pop headline", sample: "MAKE SOME NOISE", content: "MAKE SOME NOISE", style: { font: "Bebas Neue", fontSize: 112, position: "center", color: "#ffb04f", outlineColor: "#3a1d12", outlineWidth: 6, align: "center" } },
    brush: { kind: "title", label: "Signature title", sample: "Stay inspired", content: "Stay inspired", style: { font: "Dancing Script", fontSize: 120, position: "center", color: "#ffffff", outlineWidth: 0, background: "#111111", backgroundOpacity: 0.45, align: "center" } },
  };
  /** The add button uses an SVG icon: a text glyph is
 *  laid out on its baseline (a full-width "＋" also carries side bearings), so
 *  it never sits centred in the 22px box. */
const PLUS_ICON = '<svg class="asset-add-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 5.5v13M5.5 12h13"/></svg>';
  /* Reference glyph for a clip boundary: a transition mark, or a plus when none
     is set yet. */
  const JUNCTION_ICON = '<svg class="ui-icon transition" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.75a.75.75 0 0 1 1.22-.59L13.5 12l-8.28 6.84A.75.75 0 0 1 4 18.25Z" fill="currentColor" fill-opacity=".12"/><path d="M20 5.75a.75.75 0 0 0-1.22-.59L10.5 12l8.28 6.84A.75.75 0 0 0 20 18.25Z" fill="currentColor" fill-opacity=".12"/></svg>';
  const JUNCTION_PLUS = '<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5.5v13M5.5 12h13"/></svg>';
const TRANSITIONS = { fade: { label: "Fade in / out", sample: "fade" }, dip_white: { label: "Flash white", sample: "white" }, dip_black: { label: "Flash black", sample: "black" } };
  let workspaceMode = "media";
  let textLibrary = "subtitles";
  let mediaInsertQueue = Promise.resolve();
  let draggedMediaId = null;
  let transitionPreviewFrame = null;
  let transitionPreviewRestore = null;

  const escText = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  function notify(message) {
    if (typeof global.toast === "function") global.toast(message);
  }

  function currentStudio() {
    try { return typeof studio !== "undefined" ? studio : null; } catch (_) { return null; }
  }

  function currentProject() {
    try { return typeof cur !== "undefined" ? cur : null; } catch (_) { return null; }
  }

  function mediaRows() {
    const bin = document.querySelector(".studio-media-list");
    return bin ? [...bin.querySelectorAll("[data-media-id]")] : [];
  }

  function mediaInfo(id) {
    try {
      const d = typeof cur !== "undefined" ? cur.doc : null;
      const media = typeof mediaOf === "function" ? mediaOf(d).find((x) => String(x.id) === String(id)) : null;
      const clips = typeof clipsOf === "function" ? clipsOf(d) : [];
      const clip = clips.find((x) => String(x.id) === String(id)) || (media && typeof studioClipForMedia === "function" ? studioClipForMedia(media) : null);
      if (clip && (!media || media.kind !== "audio")) return { id: String(id), kind: "video", clip, media };
      if (media) return { id: String(id), kind: String(media.kind || "file"), media, clip: null };
    } catch (_) { /* the native Studio remains usable if a projection is unavailable */ }
    return { id: String(id), kind: "file", media: null, clip: null };
  }

  function activeMode(root) {
    /* An explicit tab click always wins: the tab highlight and the panel body
     resolve the mode separately, and an over-eager "no media means Media" rule
     left the highlight on Media while the body showed another tab. */
  const chosen = root.dataset.workspaceMode;
  if (chosen) return chosen;
  /* Nothing chosen yet in this session: a project without media opens on Media
     instead of whatever tab was left over from an earlier project. */
  const projectHasMedia = (() => {
    try {
      const doc = typeof cur !== "undefined" && cur ? cur.doc : null;
      const list = doc && doc.asset && Array.isArray(doc.asset.media)
        ? doc.asset.media
        : (doc && Array.isArray(doc.assets) ? doc.assets : []);
      return Array.isArray(list) && list.length > 0;
    } catch (_) {
      return false;
    }
  })();
  return workspaceMode === "text" && !projectHasMedia ? "media" : workspaceMode;
  }

  function setMode(root, mode) {
    if (MODES.some(([id]) => id === mode)) {
      workspaceMode = mode;
      root.dataset.workspaceMode = mode;
    }
  }

  function addTabs(root) {
    const head = root.querySelector(".studio-panel-head");
    if (!head) return null;
    const body = root.querySelector(".studio-bin-body");
    let tabs = head.querySelector("[data-workspace-tabs]");
    if (!tabs && body) tabs = body.querySelector("[data-workspace-tabs]");
    if (!tabs) {
      tabs = document.createElement("div");
      tabs.dataset.workspaceTabs = "true";
      tabs.className = "workspace-studio-tabs";
      (body || head).insertBefore(tabs, (body || head).firstChild || null);
    } else if (body && tabs.parentElement !== body) {
      body.insertBefore(tabs, body.firstChild || null);
    }
    const mode = activeMode(root);
    tabs.innerHTML = MODES.map(([id, label]) =>
      `<button type="button" class="workspace-studio-tab${mode === id ? " active" : ""}" data-workspace-mode="${id}" aria-pressed="${mode === id}">${label}</button>`
    ).join("");
    tabs.querySelectorAll("[data-workspace-mode]").forEach((button) => {
      button.onclick = () => switchMode(root, button.dataset.workspaceMode);
    });
    return tabs;
  }

  function nativeFilter(root, kind) {
    const button = root.querySelector(`[data-bin-filter="${kind}"]`);
    if (button) button.click();
  }

  function switchMode(root, mode) {
    setMode(root, mode);
    if (mode === "media") {
      if (root.querySelector('[data-bin-filter="all"]')) nativeFilter(root, "all");
      else if (typeof global.renderStudio === "function") global.renderStudio();
      return;
    }
    if (mode === "audio") {
      if (root.querySelector('[data-bin-filter="audio"]')) nativeFilter(root, "audio");
      else if (typeof global.renderStudio === "function") global.renderStudio();
      return;
    }
    renderWorkspaceBody(root, mode);
    /* renderTextBody/renderTransitionsBody replace .studio-bin-body wholesale,
       and the mode tabs live in that body: without re-creating them the Text
       and Transitions libraries became a dead end with no way back. */
    addTabs(root);
  }

  function mediaUpload(root) {
    let input = root.querySelector("[data-workspace-upload]");
    if (!input) {
      input = document.createElement("input");
      input.type = "file";
      input.accept = "video/*,audio/*,image/*";
      input.multiple = true;
      input.hidden = true;
      input.dataset.workspaceUpload = "true";
      root.appendChild(input);
      input.onchange = async () => {
        const project = currentProject();
        const s = currentStudio();
        if (!project?.slug || !s?.slug) return;
        const projectSlug = project.slug;
        for (const file of [...input.files]) {
          try {
            await s.persistQueue;
            if (currentProject()?.slug !== projectSlug || currentStudio()?.slug !== projectSlug) return;
            notify(`Uploading: ${file.name}`);
            if (typeof window.uploadSizeOk === "function" && !window.uploadSizeOk(file)) continue;
      const response = await fetch(`./api/project/${encodeURIComponent(projectSlug)}/upload?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || !payload.ok) throw new Error(String(response.status || "upload"));
            if (currentProject()?.slug !== projectSlug || currentStudio()?.slug !== projectSlug) return;
            if (typeof groups === "function") {
              const entry = { id: String(payload.id || (typeof stableId === "function" ? stableId("as", payload.path, payload.hash) : payload.path)), kind: payload.kind, file: payload.path, name: payload.name || file.name, origin: "upload", poster: payload.poster || null, hash: payload.hash || null, status: "active" };
              if (!groups().media.some((item) => String(item.id) === entry.id)) groups().media.push(entry);
              if (typeof normalizeClientProject === "function") normalizeClientProject(project.doc);
              if (Number.isFinite(Number(payload.rev))) {
                project.doc.rev = Number(payload.rev);
                s.rev = Number(payload.rev);
              }
            }
            notify(`Uploaded: ${file.name}`);
          } catch (_) {
            notify(`Upload failed: ${file.name}`);
          }
        }
        input.value = "";
        if (typeof renderStudio === "function") renderStudio();
      };
    }
    return input;
  }

  function prepareMediaAndInsert(info, trackId, at = null) {
    const slug = currentStudio()?.slug;
    const position = at == null ? Math.max(0, Number(currentStudio()?.playhead) || 0) : Math.max(0, Number(at) || 0);
    const insert = async () => {
      if (!slug || currentStudio()?.slug !== slug || currentProject()?.slug !== slug) return;
      await currentStudio().persistQueue;
      if (currentStudio()?.slug !== slug || currentProject()?.slug !== slug) return;
      const latest = mediaInfo(info?.id || info?.media?.id || info?.clip?.id);
      await prepareMediaNow(latest.media || latest.clip ? latest : info, trackId, position);
      if (currentStudio()?.slug === slug) await currentStudio().persistQueue;
    };
    const result = mediaInsertQueue.then(insert, insert);
    mediaInsertQueue = result.catch(() => {});
    return result;
  }

  async function prepareMediaNow(info, trackId, at) {
    const s = currentStudio();
    const project = currentProject();
    if (!s?.slug || project?.slug !== s.slug || (!info?.media?.id && !info?.clip?.id)) return;
    const projectSlug = s.slug;
    const targetId = info.kind === "audio" ? (String(trackId || "").startsWith("audio-") ? trackId : "audio-main") : (trackId || "video-main");
    if (info.kind === "audio" && typeof studioAddMedia === "function" && info.media?.id) {
      studioAddMedia(info.media.id, targetId, at);
      return;
    }
    if (!String(targetId).startsWith("video-")) return notify("Drop a video or image onto a video track");
    if (info.clip && typeof studioAddClip === "function") {
      studioAddClip(info.clip.id, targetId, at);
      return;
    }
    if (!["image", "video"].includes(info.kind)) {
      notify("This asset type cannot be added directly to a video track");
      return;
    }
    if (typeof jpost !== "function") {
      notify("Media preparation is not supported by this runtime");
      return;
    }
    const baseRev = Math.max(Number(s.rev) || 0, Number(project.doc?.rev) || 0);
    const result = await jpost(`./api/project/${encodeURIComponent(s.slug)}/media/prepare`, { media_id: String(info.media.id), base_rev: baseRev, duration: Number(info.media.duration) || undefined });
    if (currentProject()?.slug !== projectSlug || currentStudio()?.slug !== projectSlug) return;
    if (result.status === 409) {
      notify("Project updated. Select the asset again");
      return;
    }
    if (result.status < 200 || result.status >= 300 || !result.json?.ok) {
      notify(result.json?.error || "This asset cannot be added to the timeline yet");
      return;
    }
    const payload = result.json;
    if (payload.project && project) {
      project.doc = payload.project;
      if (typeof normalizeClientProject === "function") normalizeClientProject(project.doc);
    }
    if (Number.isFinite(Number(payload.rev))) s.rev = Number(payload.rev);
    if (typeof studioLoad === "function") await studioLoad(true);
    if (currentStudio()?.slug !== projectSlug || currentProject()?.slug !== projectSlug) return;
    const clip = payload.clip || payload.clip_record;
    if (clip && typeof studioAddClip === "function") studioAddClip(clip.id || clip.clip_id, targetId, at);
    else if (typeof renderStudio === "function") renderStudio();
  }

  function insertFromMedia(root, id, trackId, at = null) {
    const info = mediaInfo(id);
    return prepareMediaAndInsert(info, trackId, at).catch(() => notify("Failed to add asset to timeline"));
  }

  function bindMediaDrops(root) {
    const shell = root.closest(".studio-shell");
    if (!shell) return;
    shell.querySelectorAll("[data-lane]").forEach((lane) => {
      if (lane.dataset.workspaceMediaDrop) return;
      lane.dataset.workspaceMediaDrop = "true";
      lane.addEventListener("drop", (event) => {
        const preset = event.dataTransfer?.getData("application/x-vam-text");
        const effect = event.dataTransfer?.getData("application/x-vam-transition");
        const id = event.dataTransfer?.getData("application/x-vam-media-id") || draggedMediaId;
        if (!id && !TEXT_PRESETS[preset] && !TRANSITIONS[effect]) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        lane.style.outline = "";
        const bounds = lane.getBoundingClientRect();
        const total = typeof studioTotal === "function" ? studioTotal() : 1;
        const position = Math.max(0, (event.clientX - bounds.left) / Math.max(1, bounds.width) * total);
        draggedMediaId = null;
        if (currentStudio()) currentStudio().dragId = null;
        if (TEXT_PRESETS[preset]) addTextCue(preset, position, lane.dataset.lane);
        else if (TRANSITIONS[effect]) {
          const pair = transitionPairs().filter((item) => item.track.id === lane.dataset.lane).sort((first, second) => Math.abs(Number(first.right.start) - position) - Math.abs(Number(second.right.start) - position))[0];
          if (pair) applyTransition(effect, pair.left.id);
          else notify("Place two video clips next to each other on this track first.");
        } else insertFromMedia(root, id, lane.dataset.lane, position);
      }, true);
    });
  }

  function mediaBody(root) {
    const body = root.querySelector(".studio-bin-body");
    if (!body) return;
    const upload = mediaUpload(root);
    let button = body.querySelector("[data-workspace-import]");
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.className = "workspace-import secondary";
      button.dataset.workspaceImport = "true";
      button.textContent = "＋ Import media";
      const tabs = body.querySelector("[data-workspace-tabs]");
      body.insertBefore(button, tabs ? tabs.nextSibling : body.firstChild);
      button.onclick = () => upload.click();
    }
    body.querySelectorAll("[data-media-id]").forEach((original) => {
      if (original.dataset.workspaceBound) return;
      const card = document.createElement("article");
      card.className = `${original.className} media-tile`;
      card.dataset.mediaId = original.dataset.mediaId;
      card.draggable = true;
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", `${original.querySelector(".studio-media-name")?.textContent || "media"} — drag onto a track, or use the + button`);
      card.innerHTML = original.innerHTML;
      /* Reference parity: the duration sits at the thumbnail's bottom-left
         instead of trailing the media meta line. */
      const metaText = (original.querySelector(".studio-media-meta")?.textContent || "").trim();
      const rawDuration = metaText.includes("·") ? metaText.split("·").pop().trim() : metaText;
      const secondsFromStamp = (stamp) => {
        const parts = String(stamp).split(":").map((value) => Number(value));
        if (parts.some((value) => !Number.isFinite(value))) return 0;
        if (parts.length === 4) return parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 30;
        if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
        if (parts.length === 2) return parts[0] * 60 + parts[1];
        return parts[0] || 0;
      };
      const durationSeconds = secondsFromStamp(rawDuration);
      if (durationSeconds > 0) {
        const duration = document.createElement("span");
        duration.className = "workspace-media-duration";
        duration.textContent = `${Math.max(1, Math.round(durationSeconds))}s`;
        // The duration sits in the tile's bottom band next to the
        // add button, not over the artwork.
        card.appendChild(duration);
      }
      original.replaceWith(card);
      card.dataset.workspaceBound = "true";
      card.title = "Drag onto a track, or press + to add it at the first free time";
      const add = document.createElement("button");
      add.type = "button";
      add.className = "workspace-media-add asset-add";
      add.dataset.workspaceAddMedia = "true";
      add.setAttribute("aria-label", card.getAttribute("aria-label"));
      add.innerHTML = PLUS_ICON;
      card.appendChild(add);
      card.onclick = (event) => {
        /* The tile body has no click behaviour: media is added either
           by the + button or by dragging it onto a track. */
        if (!event.target.closest("[data-workspace-add-media]")) return;
        event.preventDefault();
        event.stopPropagation();
        if (event.detail > 1) return;
        insertFromMedia(root, card.dataset.mediaId);
      };
      card.ondblclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
      };
      card.ondragstart = (event) => {
        draggedMediaId = card.dataset.mediaId;
        if (currentStudio()) currentStudio().dragId = draggedMediaId;
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("application/x-vam-media-id", draggedMediaId);
        event.dataTransfer.setData("text/plain", draggedMediaId);
      };
      card.ondragend = () => {
        draggedMediaId = null;
        if (currentStudio()) currentStudio().dragId = null;
      };
    });
  }

  function renderTextBody(root) {
    const body = root.querySelector(".studio-bin-body");
    if (!body) return;
    body.innerHTML = `<div class="workspace-text-library"><div class="text-library-tabs"><button type="button" class="${textLibrary === "subtitles" ? "active" : ""}" data-text-category="subtitles">Subtitles</button><button type="button" class="${textLibrary === "titles" ? "active" : ""}" data-text-category="titles">Titles &amp; styled text</button></div><p class="text-library-note">${textLibrary === "subtitles" ? "Dialogue captions · bottom aligned" : "Standalone headlines · expressive styles"}</p>${Object.entries(TEXT_PRESETS).filter(([, preset]) => (preset.kind === "subtitle") === (textLibrary === "subtitles")).map(([key, preset]) => `<article class="text-template text-preset" draggable="true" data-workspace-preset="${key}"><div class="text-sample preset-${key}" style="font-family:'${preset.style.font}',${key === "title" ? "serif" : key === "brush" ? "cursive" : "sans-serif"}">${escText(preset.sample)}</div><strong>${preset.label}</strong><button type="button" class="primary" data-workspace-add-text="${key}">＋ Add ${preset.kind === "subtitle" ? "subtitle" : "title"}</button></article>`).join("")}</div>`;
    body.querySelectorAll("[data-text-category]").forEach((button) => {
      button.onclick = () => { textLibrary = button.dataset.textCategory; renderTextBody(root); };
    });
    body.querySelectorAll("[data-workspace-add-text]").forEach((button) => {
      button.onclick = () => addTextCue(button.dataset.workspaceAddText);
    });
    body.querySelectorAll("[data-workspace-preset]").forEach((card) => {
      card.ondragstart = (event) => {
        draggedMediaId = null;
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("application/x-vam-text", card.dataset.workspacePreset);
      };
    });
    /* Switching between Subtitles and Titles replaces this body, so the mode
       tabs must be re-created here as well. */
    addTabs(root);
  }

  /* Reference parity: a new caption takes the first slot at or after the
     requested time that does not overlap an existing one, so pressing
     "Add subtitle" again continues the sequence instead of covering the
     previous caption. */
  function freeCaptionStart(cues, desired, duration) {
    const ordered = (cues || []).slice().sort((a, b) => Number(a.start || 0) - Number(b.start || 0));
    let start = Math.max(0, Number(desired) || 0);
    for (const cue of ordered) {
      const cueStart = Math.max(0, Number(cue.start) || 0);
      const cueEnd = Math.max(cueStart, Number(cue.end) || cueStart);
      if (start < cueEnd && start + duration > cueStart) start = cueEnd;
    }
    return Math.round(start * 1000) / 1000;
  }

  function addTextCue(presetId = "subtitle", at = null, trackId = "text-main") {
    const s = currentStudio();
    if (!s?.timeline) return;
    if (trackId !== "text-main") return notify("Drop text onto the text track.");
    const preset = TEXT_PRESETS[presetId] || TEXT_PRESETS.subtitle;
    const track = typeof studioTrack === "function" ? studioTrack("text-main") : null;
    if (!track || typeof stableId !== "function" || typeof studioCommit !== "function") return notify("The text track is unavailable.");
    const desired = Math.max(0, Number(at == null ? s.playhead : at) || 0);
    const start = freeCaptionStart(track.cues, desired, 5);
    let sequence = (track.cues || []).length;
    let id = stableId("cue", s.slug, presetId, start, sequence);
    while ((track.cues || []).some((cue) => cue.id === id)) id = stableId("cue", s.slug, presetId, start, ++sequence);
    const cue = { id, start, end: start + 5, text: preset.content, kind: preset.kind, style: { ...preset.style } };
    if (typeof studioSetSelection === "function") studioSetSelection([cue.id], cue.id);
    studioCommit({ op: "set_caption", cue }, (timeline) => { const target = timeline.tracks.find((item) => item.id === "text-main"); if (target) (target.cues || (target.cues = [])).push(cue); });
    if (typeof global.workspaceStudioShowInspector === "function") global.workspaceStudioShowInspector("Text");
    notify(`${preset.kind === "title" ? "Title" : "Subtitle"} added. Edit its text and style in the inspector.`);
  }

  function renderTransitionsBody(root) {
    const body = root.querySelector(".studio-bin-body");
    if (!body) return;
    const pair = selectedTransitionPair();
    const transition = pair?.left?.transition_out;
    body.innerHTML = `<div class="workspace-transition-list">${Object.entries(TRANSITIONS).map(([kind, effect]) => `<article class="transition-tile${transition?.kind === kind ? " active" : ""}" draggable="true" data-workspace-effect="${kind}"><span class="transition-demo ${effect.sample}"></span><span>${effect.label}</span><button type="button" class="asset-add" data-workspace-transition="${kind}" aria-label="Add ${effect.label}">${PLUS_ICON}</button></article>`).join("")}<p class="status-note">Select a clip beside a cut, then click + to add a transition between the two clips.</p></div>`;
    body.querySelectorAll("[data-workspace-transition]").forEach((button) => {
      button.onclick = () => applyTransition(button.dataset.workspaceTransition);
    });
    body.querySelectorAll("[data-workspace-effect]").forEach((card) => {
      card.ondragstart = (event) => {
        draggedMediaId = null;
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("application/x-vam-transition", card.dataset.workspaceEffect);
      };
    });
    bindTransitionControls(body, pair);
    /* Same as the text library: this body was replaced, so the mode tabs have
       to come back before the user can leave the Transitions library. */
    addTabs(root);
  }

  function transitionControlsHtml(pair) {
    if (!pair) return '<p class="text-library-note">Add two adjacent video clips to enable transition controls.</p>';
    const transition = pair.left.transition_out;
    const duration = Math.min(pair.usable, Math.max(0.1, Number(transition?.duration) || 0.5));
    const kind = transition?.kind && TRANSITIONS[transition.kind] ? transition.kind : "fade";
    const max = Math.max(0.1, pair.usable);
    /* Reference layout: Transition / Effect / Duration (s) / Preview + Delete. */
    return `<section class="workspace-transition-controls"><h4>Transition</h4>`
      + `<label class="workspace-transition-label">Effect<select aria-label="Transition effect" data-workspace-transition-select>${Object.entries(TRANSITIONS).map(([key, effect]) => `<option value="${key}" ${kind === key ? "selected" : ""}>${effect.label}</option>`).join("")}</select></label>`
      + `<label class="workspace-transition-label">Duration (s)</label>`
      + `<div class="transition-duration"><input type="range" aria-label="Transition duration slider" min="0.1" max="${max.toFixed(2)}" step="0.1" value="${duration.toFixed(1)}" data-workspace-transition-duration><input type="number" aria-label="Transition duration in seconds" min="0.1" max="${max.toFixed(2)}" step="0.1" value="${duration.toFixed(1)}" data-workspace-transition-duration></div>`
      + `<div class="transition-actions"><button type="button" class="primary" data-workspace-transition-preview>▶ Preview</button><button type="button" class="secondary" data-workspace-transition-remove>Delete</button></div>`
      + `</section>`;
  }


  function bindTransitionControls(body, pair) {
    const transition = pair?.left?.transition_out;
    const select = body.querySelector("[data-workspace-transition-select]");
    if (select) select.onchange = () => applyTransition(select.value, pair.left.id);
    const easing = body.querySelector("[data-workspace-transition-easing]");
    if (easing) easing.onchange = () => applyTransition(transition?.kind || "fade", pair.left.id, transition?.duration, easing.value);
    body.querySelectorAll("[data-workspace-transition-duration]").forEach((input) => {
      input.oninput = () => body.querySelectorAll("[data-workspace-transition-duration]").forEach((other) => { if (other !== input) other.value = input.value; });
      input.onchange = () => {
        if (!input.checkValidity()) return input.reportValidity();
        applyTransition(transition.kind, pair.left.id, Number(input.value));
      };
    });
    const preview = body.querySelector("[data-workspace-transition-preview]");
    if (preview) preview.onclick = () => previewTransition(pair);
    const remove = body.querySelector("[data-workspace-transition-remove]");
    if (remove) remove.onclick = () => applyTransition("cut", pair.left.id);
  }

  function workspaceStudioTransitionControls(container) {
    if (!container) return;
    const pair = selectedTransitionPair();
    container.innerHTML = transitionControlsHtml(pair);
    bindTransitionControls(container, pair);
  }

  function transitionPairs() {
    const pairs = [];
    for (const track of currentStudio()?.timeline?.tracks || []) {
      if (track.kind !== "video") continue;
      const clips = (track.clips || []).slice().sort((first, second) => Number(first.start) - Number(second.start));
      for (let index = 0; index < clips.length - 1; index++) {
        const left = clips[index], right = clips[index + 1];
        /* Seam resolution: accept a small gap or overlap while
           the user is positioning clips, then let the server normalize the
           exact boundary.  The transition window stays centered on the cut
           and never exceeds half of either neighboring clip. */
        const seam = Number(left.start) + Number(left.duration);
        const delta = Number(right.start) - seam;
        /* Same tolerance the drag uses (8px of timeline), so a seam that looks
           touching always offers its junction. */
        const seamTolerance = Math.max(0.05, typeof studioSnapSeconds === "function" ? studioSnapSeconds(8) : 0.05);
        if (!Number.isFinite(delta) || Math.abs(delta) > seamTolerance) continue;
        const usable = Math.min(2, Number(left.duration) / 2, Number(right.duration) / 2);
        if (usable >= 0.05) pairs.push({ left, right, track, usable });
      }
    }
    return pairs;
  }

  function selectedTransitionPair(leftId = null) {
    const pairs = transitionPairs();
    const selected = leftId || (typeof studioSelected === "function" ? studioSelected()?.item?.id : null);
    return pairs.find((pair) => String(pair.left.id) === String(selected)) || (leftId ? null : pairs.find((pair) => String(pair.right.id) === String(selected))) || null;
  }

  function applyTransition(kind, leftId = null, duration = null, easing = null) {
    if (!TRANSITIONS[kind] && kind !== "cut") return;
    const pair = selectedTransitionPair(leftId);
    if (!pair || typeof studioCommit !== "function") return notify("Place two video clips next to each other on the same track, then select one.");
    stopTransitionPreview();
    const value = kind === "cut" ? 0 : Math.max(0.05, Math.min(pair.usable, Number(duration ?? pair.left.transition_out?.duration) || 0.5));
    const transition = { kind, duration: value, easing: kind === "fade" ? (easing || pair.left.transition_out?.easing || "linear") : "linear" };
    if (typeof studioSetSelection === "function") studioSetSelection([pair.left.id], pair.left.id);
    studioCommit({ op: "set_transition", item_id: pair.left.id, which: "out", ...transition }, (timeline) => {
      const item = timeline.tracks.flatMap((track) => track.clips || []).find((clip) => String(clip.id) === String(pair.left.id));
      if (item) item.transition_out = transition;
    }, { light: true });
    /* Repaint what a transition change actually affects. */
    global.workspaceStudioRefreshJunctions?.();
    if (typeof studioBuildReferenceInspector === "function") studioBuildReferenceInspector(studioSelected());
    if (typeof studioQueueDraw === "function") studioQueueDraw();
    if (typeof global.workspaceStudioShowInspector === "function") global.workspaceStudioShowInspector(kind === "cut" ? "Video" : "Transition");
  }

  function stopTransitionPreview() {
    if (transitionPreviewFrame != null) global.cancelAnimationFrame(transitionPreviewFrame);
    transitionPreviewFrame = null;
    if (transitionPreviewRestore) transitionPreviewRestore();
    transitionPreviewRestore = null;
  }

  function previewTransition(pair = selectedTransitionPair()) {
    const s = currentStudio();
    if (!pair || !TRANSITIONS[pair.left.transition_out?.kind] || !s || typeof studioStartPlayback !== "function" || typeof studioSetPlayhead !== "function") return;
    stopTransitionPreview();
    if (typeof studioStopPlayback === "function") studioStopPlayback(false);
    const slug = s.slug, previousLoop = s.loop;
    s.loop = false;
    transitionPreviewRestore = () => { if (currentStudio()?.slug === slug) currentStudio().loop = previousLoop; };
    const duration = Number(pair.left.transition_out.duration) || 0.3;
    const start = Math.max(Number(pair.left.start), Number(pair.right.start) - duration - 0.3);
    const end = Math.min(Number(pair.right.start) + Number(pair.right.duration), Number(pair.right.start) + duration + 0.3);
    studioSetPlayhead(start, typeof studioTotal === "function" ? studioTotal() : end);
    studioStartPlayback();
    const inspect = () => {
      const active = currentStudio();
      if (active?.slug !== slug || !active?.playing) return stopTransitionPreview();
      if (Number(active.playhead) >= end) {
        if (typeof studioStopPlayback === "function") studioStopPlayback(false);
        studioSetPlayhead(end, typeof studioTotal === "function" ? studioTotal() : end);
        return stopTransitionPreview();
      }
      transitionPreviewFrame = global.requestAnimationFrame(inspect);
    };
    transitionPreviewFrame = global.requestAnimationFrame(inspect);
  }

  function renderTransitionMarkers(root) {
    const shell = root.closest(".studio-shell");
    if (!shell) return;
    shell.querySelectorAll("[data-workspace-junction]").forEach((marker) => marker.remove());
    const total = typeof studioTotal === "function" ? studioTotal() : 1;
    const selected = typeof studioSelected === "function" ? studioSelected()?.item?.id : null;
    for (const pair of transitionPairs()) {
      const lane = [...shell.querySelectorAll("[data-lane]")].find((element) => element.dataset.lane === pair.track.id);
      if (!lane) continue;
      const effect = TRANSITIONS[pair.left.transition_out?.kind];
      const marker = document.createElement("button");
      marker.type = "button";
      const transitionOpen=typeof global.workspaceStudioInspectorTab==="function"&&global.workspaceStudioInspectorTab()==="Transition";
      marker.className = `workspace-junction${effect ? " has-transition" : ""}${transitionOpen&&String(selected) === String(pair.left.id) ? " is-selected" : ""}`;
      marker.dataset.workspaceJunction = pair.left.id;
      marker.style.left = `${Number(pair.right.start) / Math.max(0.001, total) * 100}%`;
      /* With a transition set the handle spans the transition's own length on
         the timeline (reference: max(30, duration/total * tracksWidth)), so a
         longer fade is visibly wider. */
      if (effect) {
        const span = Math.max(0.05, Number(pair.left.transition_out?.duration) || 0);
        const laneWidth = Math.max(1, lane.scrollWidth || lane.getBoundingClientRect().width || 1);
        marker.style.width = `${Math.round(Math.max(30, (span / Math.max(0.001, total)) * laneWidth))}px`;
      } else {
        marker.style.width = "";
      }
      marker.innerHTML = effect ? JUNCTION_ICON : JUNCTION_PLUS;
      marker.title = effect ? `${effect.label} · ${Number(pair.left.transition_out.duration).toFixed(2)}s` : "Add transition between clips";
      marker.setAttribute("aria-label", marker.title);
      marker.onpointerdown = (event) => event.stopPropagation();
      marker.onclick = (event) => {
        event.preventDefault(); event.stopPropagation();
        if (typeof studioSetSelection === "function") studioSetSelection([pair.left.id], pair.left.id);
        if (typeof studioSetPlayhead === "function") studioSetPlayhead(Number(pair.right.start), total);
        if (!effect) applyTransition("fade", pair.left.id);
        if (typeof studioQueueDraw === "function") studioQueueDraw();
        if (typeof global.workspaceStudioShowInspector === "function") global.workspaceStudioShowInspector("Transition");
        /* The video inspector is built by the editor module, so rebuild it for
           the Transition surface and refresh the junction rings. */
        if (typeof studioBuildReferenceInspector === "function") studioBuildReferenceInspector(studioSelected());
        else if (typeof renderStudio === "function") renderStudio();
        global.workspaceStudioRefreshJunctions?.();
      };
      marker.ondragover = (event) => {
        if (![...event.dataTransfer.types].includes("application/x-vam-transition")) return;
        event.preventDefault(); event.stopPropagation();
      };
      lane.appendChild(marker);
    }
  }

  function renderWorkspaceBody(root, mode) {
    if (mode === "text") renderTextBody(root);
    else if (mode === "transitions") renderTransitionsBody(root);
    else if (mode === "audio") mediaBody(root);
  }

  function workspaceStudioEnhance() {
    const root = document.querySelector(".studio-bin");
    if (!root) return;
    addTabs(root);
    const mode = activeMode(root);
    if (mode === "media") mediaBody(root);
    else renderWorkspaceBody(root, mode);
    bindMediaDrops(root);
    renderTransitionMarkers(root);
  }

  global.workspaceStudioEnhance = workspaceStudioEnhance;
  global.workspaceStudioPrepareMedia = prepareMediaAndInsert;
  global.workspaceStudioAddText = addTextCue;
  global.workspaceStudioApplyTransition = applyTransition;
  global.workspaceStudioPreviewTransition = previewTransition;
  global.workspaceStudioTransitionControls = workspaceStudioTransitionControls;
  /* Used by the junction click to update its selection ring in place. */
  global.workspaceStudioRefreshJunctions = () => {
    const shell = document.querySelector(".studio-shell");
    if (shell) renderTransitionMarkers(shell);
  };
})(window);
