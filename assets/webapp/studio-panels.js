(function (global) {
  "use strict";

  const panelState = { slug: null, selection: null, tab: "Video", drawer: null };
  const fonts = ["Inter", "Roboto", "Playfair Display", "Bebas Neue", "Dancing Script", "Arial", "Noto Sans", "Helvetica", "Impact"];
  const ratios = { "16:9": [1920, 1080], "9:16": [1080, 1920], "1:1": [1080, 1080], "4:5": [1080, 1350] };
  const escaped = (value) => esc(String(value ?? ""));
  const iconPaths = {
    play: "m8 5 11 7-11 7Z", pause: "M8 5v14M16 5v14", start: "M5 5v14m14-14L8 12l11 7Z", end: "M19 5v14M5 5l11 7-11 7Z",
    canvas: "M8 4H4v4m12-4h4v4m0 8v4h-4m-8 0H4v-4", video: "M4 5h16v14H4zM8 5v14M16 5v14M4 9h4M4 15h4M16 9h4M16 15h4",
    audio: "m4 10 4 0 5-4v12l-5-4H4zM17 8q6 4 0 8", text: "M4 5h16M12 5v14M8 19h8", close: "m6 6 12 12M6 18 18 6",
    undo: "m8 3-4 4 4 4M4 7h10a6 6 0 0 1 0 12H5", redo: "m16 3 4 4-4 4M20 7H10a6 6 0 0 0 0 12h9", loop: "m16 3 4 4-4 4M20 7H8a5 5 0 0 0-5 5m5 9-4-4 4-4M4 17h12a5 5 0 0 0 5-5"
  };

  function icon(name) {
    return `<svg class="studio-ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${iconPaths[name] || iconPaths.video}"/></svg>`;
  }

  function number(label, key, value, min, max, step = 1) {
    /* The unit stays in the label ("Volume (%)") and the
       control itself bare, so no suffix element is rendered. */
    return `<label class="studio-property-row"><span>${escaped(label)}</span><span class="studio-number-control"><input aria-label="${escaped(label)}" data-panel-field="${key}" type="number" value="${Number(value) || 0}" min="${min}" max="${max}" step="${step}"></span></label>`;
  }

  /* Colour palette: a 24 swatch grid anchored to the trigger.  The
     native <input type=color> popup cannot be positioned (the input is hidden
     behind the chip), so it opened at the bottom of the window. */
  const PALETTE_COLORS = ['#ffffff','#e5e7eb','#9ca3af','#6b7280','#374151','#111111','#ef4444','#f97316','#ffb04f','#facc15','#84cc16','#22c55e','#14b8a6','#06b6d4','#3b82f6','#6366f1','#8b5cf6','#d946ef','#ec4899','#fda4af','#fed7aa','#fef3c7','#a7f3d0','#bfdbfe'];
  let colorPalette = null, colorTrigger = null;
  function closeColorPalette(restore = true) {
    if (colorPalette) colorPalette.remove();
    colorPalette = null;
    const button = colorTrigger;
    colorTrigger = null;
    if (button) {
      button.setAttribute("aria-expanded", "false");
      if (restore && button.isConnected) button.focus();
    }
  }
  function openColorPalette(input, button) {
    closeColorPalette(false);
    colorTrigger = button;
    button.setAttribute("aria-expanded", "true");
    const panel = document.createElement("div");
    colorPalette = panel;
    panel.className = "color-palette";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "24 common colors");
    const current = String(input.value || "").toLowerCase();
    panel.innerHTML = `<div class="color-palette-header"><strong>24 colors</strong><button type="button" aria-label="Close color palette">×</button></div>`
      + `<div class="color-palette-grid">${PALETTE_COLORS.map((value) => `<button type="button" class="palette-swatch" data-color="${value}" aria-label="${value.toUpperCase()}" aria-pressed="${current === value}" title="${value.toUpperCase()}" style="background:${value}">${current === value ? "✓" : ""}</button>`).join("")}</div>`;
    /* A <dialog> establishes the containing block for position:fixed, so clamp
       against that box instead of the viewport; otherwise the palette hangs off
       the panel even though the viewport maths look correct. */
    const host = input.closest("dialog") || document.body;
    host.append(panel);
    requestAnimationFrame(() => {
      if (colorPalette !== panel) return;
      const rect = button.getBoundingClientRect();
      const hostRect = host === document.body ? null : host.getBoundingClientRect();
      const box = hostRect
        ? { left: hostRect.left, top: hostRect.top, right: hostRect.right, bottom: hostRect.bottom }
        : { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
      const pad = 8;
      const pw = panel.offsetWidth, ph = panel.offsetHeight;
      let left = rect.right - pw;
      left = Math.max(box.left + pad, Math.min(left, box.right - pw - pad));
      let top = rect.bottom + 6;
      if (top + ph > box.bottom - pad) top = rect.top - ph - 6;
      top = Math.max(box.top + pad, Math.min(top, box.bottom - ph - pad));
      panel.style.left = (left - box.left) + "px";
      panel.style.top = (top - box.top) + "px";
    });
    panel.querySelector('[aria-label="Close color palette"]').onclick = () => closeColorPalette();
    panel.querySelectorAll("[data-color]").forEach((swatch) => {
      swatch.onclick = () => {
        input.value = swatch.dataset.color;
        closeColorPalette(false);
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
      };
    });
    if (!window.__vamColorPaletteClosers) {
      window.__vamColorPaletteClosers = true;
      document.addEventListener("pointerdown", (event) => { if (colorPalette && !colorPalette.contains(event.target) && !colorTrigger?.contains(event.target)) closeColorPalette(false); }, true);
      document.addEventListener("keydown", (event) => { if (colorPalette && event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); closeColorPalette(); } }, true);
      document.addEventListener("scroll", (event) => { if (colorPalette && !colorPalette.contains(event.target)) closeColorPalette(false); }, true);
      window.addEventListener("resize", () => closeColorPalette(false));
    }
  }

  function color(label, key, value) {
    /* The trigger shows the swatch plus the hex value; the
       hidden colour input keeps the existing data-panel-field wiring. */
    const hex = /^#[0-9a-f]{6}$/i.test(value || "") ? String(value).toLowerCase() : "#ffffff";
    return `<div class="studio-property-row studio-color-row"><span>${escaped(label)}</span><button type="button" class="studio-color-trigger" data-color-trigger="${key}"><span class="studio-color-chip" style="background:${hex}" aria-hidden="true"></span><output>${hex.toUpperCase()}</output></button><input type="color" class="studio-color-input" aria-label="${escaped(label)}" data-panel-field="${key}" value="${hex}"></div>`;
  }

  function options(values, selected) {
    return values.map((value) => `<option value="${escaped(value)}" ${String(value) === String(selected) ? "selected" : ""}>${escaped(value)}</option>`).join("");
  }

  function section(title, content) {
    return `<section class="studio-property-section"><h4>${escaped(title)}</h4>${content}</section>`;
  }

  function volumePercentOf(item) {
    const gain = Number(item.gain || 0);
    return Math.max(0, Math.min(200, Math.round(Math.pow(10, gain / 20) * 100)));
  }

  /* The same three timing rows appear on every tab. */
  function timing(picked) {
    const item = picked.item;
    const start = Math.max(0, Number(item.start) || 0);
    const duration = picked.track.kind === "subtitle"
      ? Math.max(.05, Number(item.end || 0) - start)
      : Math.max(.05, Number(item.duration) || .05);
    /* Volume only means something for media clips: the caption
       tabs (Text / Animation) carry the two timing rows alone. */
    const volume = picked.track.kind === "subtitle"
      ? ""
      : number("Volume (%)", "volume-percent", volumePercentOf(item), 0, 200, 1);
    return section("Timing",
      number("Start (s)", "start", start, 0, 86400, .1)
      + number("Duration (s)", picked.track.kind === "subtitle" ? "cue:duration" : "duration", duration, .1, 86400, .1)
      + volume);
  }

  function videoControls(picked, title) {
    const transform = picked.item.transform || {};
    const scale = Number(transform.scale ?? 1) * 100;
    return section(title, `<label class="studio-property-row studio-scale-row"><span>Scale</span><input aria-label="Scale slider" data-panel-range="scale" type="range" min="10" max="200" value="${scale}"><span class="studio-number-control"><input aria-label="Scale" data-panel-field="scale-percent" type="number" min="10" max="200" step="1" value="${scale}"></span></label>`
      + number("Position X", "x", transform.x, -8192, 8192)
      + number("Position Y", "y", transform.y, -8192, 8192)
      + number("Rotation", "rotate", transform.rotate, -180, 180));
  }

  function audioControls(picked) {
    return '<section class="studio-property-section"><p class="studio-property-note">Use the track speaker to mute audio during preview.</p></section>';
  }

  /* Font menu wording ("Inter · Regular"). */
  const FONT_STYLES = { "Inter": "Regular", "Roboto": "Regular", "Playfair Display": "Serif", "Bebas Neue": "Display", "Dancing Script": "Script" };
  function fontLabel(name) {
    const base = String(name || "");
    return FONT_STYLES[base] ? `${base} · ${FONT_STYLES[base]}` : base;
  }

  /* Caption field order: Text (button + hint), Timing, the role
     badge, then Typography / Outline / Background / Position. */
  function textControls(picked) {
    const item = picked.item;
    const style = studioCaptionStyleOf(item);
    const positionX = Number(item.style?.posX ?? 50);
    const positionY = Number(item.style?.posY ?? ({ top: 16, center: 50, bottom: 84 }[style.position] || 84));
    const fontOptions = [...new Set([...fonts, style.font])];
    const fontSelect = `<label class="studio-property-row studio-font-row"><span>Font</span><select aria-label="Text font" data-panel-field="style:font">${fontOptions.map((name) => `<option value="${escaped(name)}" ${name === style.font ? "selected" : ""}>${escaped(fontLabel(name))}</option>`).join("")}</select></label>`;
    return section("Text", `<button type="button" class="studio-property-button" data-panel-edit-text>Edit text</button><p class="studio-property-note">Double-click the timeline clip to change its content.</p>`)
      + timing(picked)
      + `<div class="studio-text-role">${item.kind === "title" ? "TITLE / DISPLAY TEXT" : "DIALOGUE SUBTITLE"}</div>`
      + section("Typography", fontSelect + number("Size", "style:fontSize", style.fontSize, 12, 240) + color("Text color", "style:color", style.color))
      + section("Outline", number("Width (px)", "style:outlineWidth", style.outlineWidth, 0, 12, 1) + color("Color", "style:outlineColor", style.outlineColor))
      + section("Background", `<label class="studio-property-row"><span>Enable background</span><input aria-label="Enable text background" type="checkbox" data-panel-background ${style.background ? "checked" : ""}></label>` + color("Color", "style:background", style.background || "#111111") + number("Opacity (%)", "background-percent", style.backgroundOpacity * 100, 0, 100))
      + section("Position", `<div class="studio-text-position-grid">${[16, 50, 84].flatMap((vertical) => [15, 50, 85].map((horizontal) => `<button type="button" data-panel-text-position="${horizontal},${vertical}" aria-label="${vertical === 16 ? "Top" : vertical === 50 ? "Middle" : "Bottom"} ${horizontal === 15 ? "left" : horizontal === 50 ? "center" : "right"}" aria-pressed="${positionX === horizontal && positionY === vertical}"><span></span></button>`)).join("")}</div>` + number("X (%)", "style:posX", positionX, 5, 95) + number("Y (%)", "style:posY", positionY, 5, 95));
  }

  /* "Edit text" dialog copy and chrome. */
  function editTextDialog() {
    const picked = studioSelected();
    if (!picked || picked.track.kind !== "subtitle") return null;
    const dialog = openDialog("Edit text",
      `<label class="studio-dialog-field"><span>Text</span><textarea name="text" rows="4" maxlength="2000" class="studio-dialog-textarea" aria-label="Text">${escaped(picked.item.text || "")}</textarea></label><p class="studio-dialog-hint">Animation, curve and timing can be adjusted in the inspector.</p>`,
      "Apply text",
      async (data, dialog) => {
        const value = String(data.get("text") || "").trim();
        if (!value) throw new Error("Enter the subtitle text.");
        changeField("caption", value);
        dialog.close();
      });
    const area = dialog.querySelector("textarea");
    if (area) { area.focus(); try { area.setSelectionRange(area.value.length, area.value.length); } catch (_) {} }
    return dialog;
  }

  /* Every tab ends with the shared Timing rows and the transition
     block; the extra per-kind action buttons are not part of that surface. */
  function advancedControls(picked, skipTiming) {
    if (!picked) return "";
    /* The Animation tab ends at the curve controls, and caption clips never
       show the transition block (video clips only). */
    if (skipTiming || picked.track.kind === "subtitle") return "";
    const controls = [timing(picked)];
    /* Reference parity: the control appears only when the next clip is adjacent;
       otherwise the section explains how to create one. */
    const transitionReady = Boolean(global.studioTransitionContext?.(picked)?.adjacent);
    controls.push(`<section class="studio-property-section"><h4>Transition between clips</h4>${transitionReady ? '<button class="studio-property-button" type="button" data-panel-transitions>＋ Add transition</button>' : '<p class="studio-property-note">Add an adjacent video clip to create a transition.</p>'}</section>`);
    return controls.join("");
  }

  function commitTextStyle(patch) {
    const picked = studioSelected();
    if (picked?.track.kind !== "subtitle") return;
    const cue = { ...studioCopy(picked.item), style: { ...(picked.item.style || {}), ...patch } };
    studioCommit({ op: "set_caption", cue }, (timeline) => {
      const track = timeline.tracks.find((entry) => entry.id === picked.track.id);
      const position = track.cues.findIndex((entry) => entry.id === cue.id);
      track.cues[position] = cue;
    });
  }

  function changeField(key, value) {
    const picked = studioSelected();
    if (!picked) return;
    if (key === "style:easing") return commitTextStyle({ easing: String(value) });
    if (key === "style:motion") return commitTextStyle({ motion: String(value) });
    if (key === "cue:duration") {
      const pickedCue = studioSelected();
      if (!pickedCue) return;
      return studioApplyField("end", Number(pickedCue.item.start || 0) + Math.max(.01, Number(value)));
    }
    if (["style:posX", "style:posY", "style:animation"].includes(key)) return commitTextStyle({ [key.slice(6)]: Number(value) });
    if (key === "volume-percent") {
      const percent = Math.max(0, Math.min(200, Number(value)));
      return studioApplyField("audio:gain", percent <= 0 ? -60 : Math.round(20 * Math.log10(percent / 100) * 100) / 100);
    }
    if (key === "scale-percent") return studioApplyField("scale", Number(value) / 100);
    if (key === "opacity-percent") return studioApplyField("opacity", Number(value) / 100);
    if (key === "background-percent") return studioApplyField("style:backgroundOpacity", Number(value) / 100);
    const error = typeof studioFieldError === "function" ? studioFieldError(key, value, picked.item) : null;
    if (error) return toast(error);
    studioApplyField(key, value);
  }

  /* The transport shows frame-accurate timecode (00:00:18:14) instead
     of the compact 0:18 form.  The host keeps writing its own compact text on
     every playhead change, so repaint the two time spans whenever it does. */
  function formatTimecode(seconds) {
    const fps = Math.max(1, Number(typeof studioFrameRate === "function" ? studioFrameRate() : 30) || 30);
    const total = Math.max(0, Number(seconds) || 0);
    const whole = Math.floor(total);
    const pad = (value) => String(Math.max(0, Math.floor(value))).padStart(2, "0");
    return `${pad(whole / 3600)}:${pad((whole % 3600) / 60)}:${pad(whole % 60)}:${pad((total - whole) * fps)}`;
  }

  function paintTimecode() {
    const duration = Math.max(
      0,
      (typeof studioPlaybackDuration === "function" ? Number(studioPlaybackDuration()) : 0) || 0,
    ) || (typeof studioTotal === "function" ? Number(studioTotal()) : 0) || 0;
    const current = formatTimecode(Math.min(Number(studio.playhead) || 0, duration || 0));
    const total = formatTimecode(duration);
    document.querySelectorAll(".studio-current-time").forEach((node) => {
      if (node.textContent !== current) node.textContent = current;
    });
    document.querySelectorAll(".studio-total-time").forEach((node) => {
      if (node.textContent !== total) node.textContent = total;
    });
  }

  function watchTimecode() {
    const host = document.querySelector("#studio-time") || document.querySelector(".studio-time");
    if (host && !host.__vamTimecodeObserved) {
      host.__vamTimecodeObserved = true;
      try {
        new MutationObserver(() => paintTimecode()).observe(host, {
          childList: true,
          characterData: true,
          subtree: true,
        });
      } catch (_) {
        /* MutationObserver unavailable: the timecode is painted on render only. */
      }
    }
    paintTimecode();
  }

  /* Number fields get a two-button stepper and sliders paint a filled
     track, so the inspector stays consistent. */
  function decorateNumberInputs(root) {
    if (!root) return;
    root.querySelectorAll("input[type=range]").forEach((range) => {
      if (range.__vamRangePainted) return;
      range.__vamRangePainted = true;
      const paint = () => {
        const min = Number(range.min || 0);
        const max = Number(range.max || 100);
        const percent = max > min
          ? Math.max(0, Math.min(100, ((Number(range.value) - min) / (max - min)) * 100))
          : 0;
        range.style.setProperty("--range-progress", `${percent}%`);
      };
      paint();
      range.addEventListener("input", paint);
    });
    root.querySelectorAll("input[type=number]").forEach((input) => {
      if (input.parentElement?.classList.contains("number-control")) return;
      const label = input.getAttribute("aria-label") || input.closest("label")?.textContent.trim() || "value";
      const wrapper = document.createElement("span");
      wrapper.className = "number-control";
      input.before(wrapper);
      wrapper.append(input);
      const stepper = document.createElement("span");
      stepper.className = "number-stepper";
      for (const [direction, word] of [[1, "Increase"], [-1, "Decrease"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "number-step";
        button.setAttribute("aria-label", `${word} ${label}`);
        button.tabIndex = -1;
        button.innerHTML = `<svg viewBox="0 0 12 8" aria-hidden="true"><path d="${direction === 1 ? "M2 6 6 2l4 4" : "m2 2 4 4 4-4"}"/></svg>`;
        button.onpointerdown = (event) => event.preventDefault();
        button.onclick = (event) => {
          event.preventDefault();
          event.stopPropagation();
          if (direction === 1) input.stepUp();
          else input.stepDown();
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
        };
        stepper.append(button);
      }
      wrapper.append(stepper);
    });
  }

  /* Reference-parity text animation tab: an easing-curve preview, the curve
     select and the fade duration.  The curve is stored on the caption style
     and drives both the live overlay (CSS timing function) and the export. */
  /* The kind select goes beyond a plain fade, while the curve and the
     duration keep their usual meaning. */
  const TEXT_MOTIONS = [
    ["fade", "Fade in / out"],
    ["none", "None (appear)"],
    ["slide-up", "Slide up"],
    ["slide-down", "Slide down"],
    ["slide-left", "Slide left"],
    ["slide-right", "Slide right"],
    ["pop", "Pop (scale)"],
  ];

  const TEXT_CURVES = [
    ["linear", "Linear"],
    ["ease-in", "Ease in"],
    ["ease-out", "Ease out"],
    ["ease-in-out", "Ease in / out"],
  ];

  function curveProgress(t, curve) {
    if (curve === "ease-in") return t * t;
    if (curve === "ease-out") return 1 - (1 - t) * (1 - t);
    if (curve === "ease-in-out") return t < 0.5 ? 2 * t * t : 1 - 2 * (1 - t) * (1 - t);
    return t;
  }

  function curvePath(curve) {
    const points = [];
    for (let i = 0; i <= 20; i += 1) {
      const t = i / 20;
      points.push(`${i ? "L" : "M"}${i * 10} ${75 - curveProgress(t, curve) * 70}`);
    }
    return points.join(" ");
  }

  function animationControls(picked) {
    const cue = picked.item;
    const heading = section("Text animation", '<p class="studio-property-note">Adjust the fade duration and motion curve.</p>') + timing(picked);
    const style = cue.style || {};
    const curve = String(style.easing || "linear");
    const span = Math.max(0.1, Number(cue.end || 0) - Number(cue.start || 0));
    const maxFade = Math.max(0.1, Math.min(2, span / 2));
    const motion = String(style.motion || "fade");
    return heading + section("Animation",
      `<label class="studio-property-row"><span>Effect</span><select data-panel-field="style:motion" aria-label="Text animation effect">${
        TEXT_MOTIONS.map(([value, label]) => `<option value="${value}" ${motion === value ? "selected" : ""}>${label}</option>`).join("")
      }</select></label>`)
      + section("Animation curve",
      `<div class="studio-curve-preview"><svg viewBox="0 0 200 80" aria-hidden="true">
         <path class="curve-grid" d="M0 0v80h200M0 40h200M100 0v80"/>
         <path class="curve-line" d="${curvePath(curve)}"/>
       </svg></div>`
      + `<label class="studio-property-row"><span>Curve</span><select data-panel-field="style:easing" aria-label="Animation curve">${
        TEXT_CURVES.map(([value, label]) => `<option value="${value}" ${curve === value ? "selected" : ""}>${label}</option>`).join("")
      }</select></label>`
      + number("Fade duration (s)", "style:animation", Math.min(Number(style.animation ?? 0.4), maxFade), 0, maxFade, .1));
  }

  function inspector() {
    const host = document.querySelector(".studio-inspector");
    if (!host || !studio.timeline) return;
    /* The host renders its own reference-styled inspector for video clips
       (Video/Audio/Speed/Adjust tabs, Transform + Appearance sections and the
       "Edit with AI" box).  Rebuilding it here replaced that surface with the
       legacy property panel, which is why the right column looked nothing
       like the editor.  Leave the host's surface untouched. */
    if (host.querySelector(".studio-inspector-body.reference-inspector")) return;
    const picked = studioSelected();
    const key = picked?.item.id || null;
    if (panelState.selection !== key || panelState.slug !== studio.slug) {
      panelState.tab = picked?.track.kind === "subtitle" ? "Text" : picked?.track.kind === "audio" ? "Audio" : "Video";
      panelState.selection = key;
      panelState.slug = studio.slug;
    }
    const tabs = picked?.track.kind === "subtitle" ? ["Text", "Animation"] : picked?.track.kind === "audio" ? ["Audio", "Speed"] : ["Video", "Audio", "Speed", "Adjust"];
    if (panelState.tab === "Transition" && picked?.track.kind === "video") tabs.splice(0, tabs.length, "Transition");
    if (!tabs.includes(panelState.tab)) panelState.tab = tabs[0];
    const title = picked ? (picked.item.text || clipsOf().find((clip) => clip.id === picked.item.clip_id)?.title || studioMediaFor(picked.item)?.name || picked.item.clip_id || "Clip") : "";
    /* The inspector head names the TRACK ("video 1"), not the clip; the clip
       name is the heading of the Video/Adjust tabs. */
    const trackDef = (typeof studioVisibleTrackDefinitions === "function" ? studioVisibleTrackDefinitions() : []).find((def) => def.id === picked?.track.id);
    const trackRaw = String(trackDef?.label || picked?.track.id || "").toLowerCase();
    /* Sentence case, as in the track head ("Subtitles 1"). */
    const trackLabel = trackRaw ? trackRaw.charAt(0).toUpperCase() + trackRaw.slice(1) : "";
    let content = '<div class="studio-inspector-empty">Select a clip to edit its properties.</div>';
    if (picked) {
      if (panelState.tab === "Video") content = videoControls(picked, title);
      else if (panelState.tab === "Audio") content = audioControls(picked);
      else if (panelState.tab === "Speed") content = `<section class="studio-property-section"><p class="studio-property-note">Playback speed controls the preview only.</p><label class="studio-property-row"><span>Preview speed</span><select aria-label="Preview speed" data-panel-field="speed">${[.5, 1, 1.5, 2].map((speed) => `<option value="${speed}" ${Number(picked.item.speed || 1) === speed ? "selected" : ""}>${speed}</option>`).join("")}</select></label></section>`;
      else if (panelState.tab === "Adjust") content = section(title, number("Opacity", "opacity-percent", Number(picked.item.transform?.opacity ?? 1) * 100, 5, 100, 1));
      else if (panelState.tab === "Text") content = textControls(picked);
      else if (panelState.tab === "Animation") content = animationControls(picked);
      else if (panelState.tab === "Transition") content = '<div data-panel-transition-host></div>';
    }
    host.innerHTML = `<div class="studio-inspector-tabs" role="tablist" aria-label="Clip properties">${tabs.map((tab) => `<button role="tab" aria-selected="${tab === panelState.tab}" class="${tab === panelState.tab ? "active" : ""}" data-panel-tab="${tab}">${tab}</button>`).join("")}<button type="button" class="studio-drawer-close" aria-label="Close inspector" data-panel-close>${icon("close")}</button></div><div class="studio-inspector-body">${picked ? `<div class="studio-selected-track">${icon(picked.track.kind === "subtitle" ? "text" : picked.track.kind)}<strong title="${escaped(title)}">${escaped(trackLabel)}</strong><span class="studio-selected-kind">Selected clip</span></div>` : ""}${content}${advancedControls(picked, panelState.tab === "Animation")}</div>`;
    host.querySelectorAll("[data-panel-tab]").forEach((button) => { button.onclick = () => { panelState.tab = button.dataset.panelTab; inspector(); }; });
    host.querySelectorAll("[data-panel-field]").forEach((control) => {
      /* Reference feel: a caption's timing field moves its block while it is
         being typed.  This only previews (model + geometry + overlay); the
         change event below still performs the single persisted commit. */
      const previewTiming = () => {
        const key = control.dataset.panelField;
        if (key !== "cue:duration" && key !== "start") return;
        if (!control.checkValidity()) return;
        const pickedNow = studioSelected();
        if (pickedNow?.track?.kind !== "subtitle") return;
        const item = pickedNow.item;
        const value = Number(control.value);
        if (!Number.isFinite(value)) return;
        if (key === "cue:duration") {
          item.end = Math.max(.01, Number(item.start || 0) + Math.max(.01, value));
        } else {
          const length = Math.max(.01, Number(item.end || 0) - Number(item.start || 0));
          item.start = Math.max(0, value);
          item.end = item.start + length;
        }
        if (typeof studioSyncTimelineItemGeometry === "function") studioSyncTimelineItemGeometry(item, pickedNow.track);
        if (typeof studioRenderCaptionOverlay === "function") studioRenderCaptionOverlay();
      };
      control.oninput = () => previewTiming();
      control.onchange = () => {
        if (!control.checkValidity()) return control.reportValidity();
        changeField(control.dataset.panelField, control.value);
      };
    });
    host.querySelectorAll("[data-panel-range]").forEach((control) => {
      const keyName = control.dataset.panelRange === "scale" ? "scale-percent" : "audio:gain";
      control.oninput = () => { const field = host.querySelector(`[data-panel-field="${keyName}"]`); if (field) field.value = control.value; };
      control.onchange = () => changeField(keyName, control.value);
    });
    host.querySelectorAll("[data-panel-text-position]").forEach((button) => { button.onclick = () => { const [posX, posY] = button.dataset.panelTextPosition.split(",").map(Number); commitTextStyle({ posX, posY }); }; });
    host.querySelectorAll("[data-panel-pip]").forEach((button) => { button.onclick = () => studioPipPresetPos(button.dataset.panelPip); });
    host.querySelectorAll("[data-panel-pip-size]").forEach((button) => { button.onclick = () => studioPipPresetSize(button.dataset.panelPipSize); });
    host.querySelector("[data-panel-background]")?.addEventListener("change", (event) => commitTextStyle({ background: event.target.checked ? "#111111" : "", backgroundOpacity: .45 }));
    host.querySelector("[data-panel-ai]")?.addEventListener("click", () => {
      const pickedNow = studioSelected();
      const titleNow = pickedNow?.item?.text || clipsOf().find((clip) => clip.id === pickedNow?.item?.clip_id)?.title || studioMediaFor(pickedNow?.item)?.name || "selected clip";
      if (typeof global.intentDraft === "function") global.intentDraft(`Edit the selected video clip “${titleNow}”: `);
      else if (typeof global.toast === "function") global.toast("Assistant draft is ready in the conversation.");
    });
    host.querySelector("[data-panel-transitions]")?.addEventListener("click", () => global.workspaceStudioShowInspector("Transition"));
    host.querySelector("[data-panel-edit-text]")?.addEventListener("click", () => editTextDialog());
    host.querySelectorAll("[data-color-trigger]").forEach((trigger) => {
      const input = host.querySelector(`.studio-color-input[data-panel-field="${trigger.dataset.colorTrigger}"]`);
      if (!input) return;
      trigger.addEventListener("click", () => openColorPalette(input, trigger));
      const paint = () => {
        const chip = trigger.querySelector(".studio-color-chip");
        const output = trigger.querySelector("output");
        if (chip) chip.style.background = input.value;
        if (output) output.textContent = String(input.value).toUpperCase();
      };
      input.addEventListener("input", paint);
      input.addEventListener("change", paint);
    });
    host.querySelector("[data-panel-mute]")?.addEventListener("click", () => {
      const muted = !picked.track.muted;
      studioCommit({ op: "set_track", track_id: picked.track.id, muted }, (timeline) => { timeline.tracks.find((track) => track.id === picked.track.id).muted = muted; });
    });
    host.querySelector("[data-panel-reset-transform]")?.addEventListener("click", () => {
      const transform = { x: 0, y: 0, scale: picked.track.id === "video-overlay" ? .35 : 1, rotate: 0, opacity: 1, border: 0 };
      studioCommit({ op: "set_transform", item_id: picked.item.id, transform }, (timeline) => { timeline.tracks.find((track) => track.id === picked.track.id).clips.find((item) => item.id === picked.item.id).transform = transform; });
    });
    host.querySelector("[data-panel-ripple]")?.addEventListener("click", studioRippleDelete);
    host.querySelector("[data-panel-close-gap]")?.addEventListener("click", studioCloseGap);
    host.querySelector("[data-panel-close]")?.addEventListener("click", () => setDrawer(null));
    const transitionHost = host.querySelector("[data-panel-transition-host]");
    if (transitionHost && global.workspaceStudioTransitionControls) global.workspaceStudioTransitionControls(transitionHost);
  }

  function openDialog(title, content, action, submit) {
    document.querySelector(".studio-dialog")?.close();
    const previous = document.activeElement;
    const dialog = document.createElement("dialog");
    dialog.className = "studio-dialog";
    dialog.setAttribute("aria-labelledby", "studio-dialog-title");
    const referenceDialog = ["Canvas settings", "Add track", "Export video", "Edit text", "Rename project"].includes(title);
    dialog.classList.toggle("studio-reference-dialog", referenceDialog);
    dialog.innerHTML = referenceDialog
      ? `<form><h2 id="studio-dialog-title">${escaped(title)}</h2>${content}<p class="studio-dialog-error" role="alert" hidden></p><footer><button type="button" data-dialog-close>Cancel</button><button type="submit" class="primary">${escaped(action)}</button></footer></form>`
      : `<form><header><h2 id="studio-dialog-title">${escaped(title)}</h2><button type="button" data-dialog-close aria-label="Close ${escaped(title)}">${icon("close")}</button></header>${content}<p class="studio-dialog-error" role="alert" hidden></p><footer><button type="button" data-dialog-close>Cancel</button><button type="submit" class="primary">${escaped(action)}</button></footer></form>`;
    /* Focus rings are class-driven so they do not depend on the :focus
       pseudo-class (which browsers only match while the window is focused). */
    dialog.querySelectorAll("select").forEach((select) => {
      select.addEventListener("focus", () => select.classList.add("is-focused"));
      select.addEventListener("blur", () => select.classList.remove("is-focused"));
    });
    dialog.querySelectorAll("[data-dialog-close]").forEach((button) => { button.onclick = () => dialog.close(); });
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener("close", () => { dialog.remove(); if (previous?.isConnected) previous.focus({ preventScroll: true }); });
    dialog.addEventListener("keydown", (event) => event.stopPropagation());
    dialog.querySelector("form").onsubmit = async (event) => {
      event.preventDefault();
      const error = dialog.querySelector(".studio-dialog-error");
      error.hidden = true;
      try { await submit(new FormData(event.currentTarget), dialog); }
      catch (failure) { error.textContent = failure.message || "The operation could not be completed. Please retry."; error.hidden = false; }
    };
    document.body.append(dialog);
    dialog.showModal();
    // The dialog opens clean and the orange ring appears
    // only once the select is clicked or tabbed into.  Chrome autofocuses the
    // first control inside a modal dialog, so the focus is handed to the
    // dialog itself instead.
    try {
      const first = dialog.querySelector("select, input, textarea, button");
      if (first && document.activeElement === first) {
        dialog.tabIndex = -1;
        dialog.focus({ preventScroll: true });
      }
    } catch (error) { /* focus is cosmetic: never block the dialog */ }
    return dialog;
  }

  function canvasDialog() {
    const slug = studio.slug;
    const canvas = studio.timeline.canvas || {};
    openDialog("Canvas settings", `<label>Aspect ratio<select name="ratio">${options(Object.keys(ratios), canvas.ratio || "9:16")}</select></label><label for="studio-canvas-background">Background color</label><div class="canvas-color-row"><input id="studio-canvas-background" type="color" name="background" aria-label="Choose canvas background color" value="${escaped(canvas.background || "#101114")}"><div><output for="studio-canvas-background" id="studio-canvas-color-value">${String(canvas.background || "#101114").toUpperCase()}</output><span>Click the swatch to choose a color</span></div></div>`, "Apply canvas", async (data, dialog) => {
      if (studio.slug !== slug) throw new Error("The active project changed. Reopen Canvas settings.");
      const ratio = data.get("ratio");
      const [width, height] = ratios[ratio];
      const background = String(data.get("background"));
      const next = studioCopy(studio.timeline);
      next.canvas = { ...next.canvas, ratio, width, height, background };
      studioCommit({ op: "replace_timeline", timeline: next }, (timeline) => { timeline.canvas = next.canvas; });
      dialog.close();
      // Apply the canvas shell immediately as well as persisting the timeline.
      // The compositor redraw follows on the next frame, so the user never
      // sees the previous ratio/background while the save request is queued.
      requestAnimationFrame(() => {
        const shell = document.querySelector(".studio-canvas");
        if (shell) {
          shell.dataset.ratio = ratio;
          shell.style.background = background;
          shell.style.aspectRatio = `${width} / ${height}`;
        }
        if (typeof studioDrawCanvas === "function") studioDrawCanvas();
      });
    });
    const color = document.querySelector("#studio-canvas-background");
    const value = document.querySelector("#studio-canvas-color-value");
    if (color && value) { const update = () => { value.textContent = color.value.toUpperCase(); }; color.addEventListener("input", update); color.addEventListener("change", update); }
  }

  async function exportVideo(data, dialog, slug) {
    const submit = dialog.querySelector('button[type="submit"]');
    const status = dialog.querySelector(".studio-export-status");
    if (studio.slug !== slug) throw new Error("The active project changed. Reopen Export video.");
    submit.disabled = true;
    try {
      await studio.persistQueue;
      const ratio = studio.timeline.canvas.ratio;
      const base = ratios[ratio] || ratios["16:9"];
      // The export dialog only asks for a quality tier: output size and
      // frame rate are shown as facts, so the export renders at the canvas size
      // and the timeline frame rate.
      /* The radio carries the CRF; the project remembers the tier, and the tier is
         what drives the bitrate table.  Sending a fixed "high" made every export a
         1.5x-bitrate file and overwrote the saved choice on each run. */
      const chosenCrf = Number(data.get("quality"));
      const chosenQuality = chosenCrf === 18 ? "high" : chosenCrf === 28 ? "smaller" : "recommended";
      const preset = { ratio, width: Math.round(base[0] / 2) * 2, height: Math.round(base[1] / 2) * 2, fps: Number(studioFrameRate()) || 30, format: "mp4", quality: chosenQuality, crf: chosenCrf };
      status.textContent = "Preparing export…";
      let result;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        result = await jpost(`./api/project/${encodeURIComponent(slug)}/render`, { timeline_rev: studio.rev, mode: "export", preset });
        if (result.status !== 409 || attempt === 1) break;
        const latest = await jget(`./api/project/${encodeURIComponent(slug)}`);
        studio.rev = Number(latest.project?.rev ?? studio.rev);
        if (cur.slug === slug && cur.doc) cur.doc.rev = studio.rev;
      }
      if (result.status >= 400) throw new Error(result.json?.error || "Export could not start. Please retry.");
      const jobId = result.json.job_id || result.json.id || result.json.job?.id;
      let completed = result.json;
      if (jobId) {
        studio.renderJobs[jobId] = { mode: "export", automatic: false };
        try {
          for (let attempt = 0; attempt < 600; attempt += 1) {
            completed = await jget(`./api/project/${encodeURIComponent(slug)}/render/${encodeURIComponent(jobId)}`);
            const state = String(completed.status || completed.job?.status || completed.state || "");
            if (["failed", "error"].includes(state)) throw new Error(completed.error || completed.job?.error || "The export failed. Your timeline is unchanged.");
            if (["completed", "done", "success"].includes(state) || completed.done === true || completed.job?.done === true) break;
            if (attempt === 599) throw new Error("Export is still running. You can find the finished file in this project's exports.");
            status.textContent = "Rendering video… You can close this window; the export will continue.";
            await new Promise((resolve) => setTimeout(resolve, 1000));
          }
        } finally { delete studio.renderJobs[jobId]; }
      }
      const file = completed.file || completed.output || completed.output_file || completed.job?.file || completed.job?.output;
      if (!file) throw new Error("The export did not return a video file.");
      if (cur.slug === slug) await studioRenderComplete(completed, "export");
      status.textContent = "Export complete. Your video is saved in the project.";
      /* Downloads live in the home Exports view now, and the dialog closes from its
         top-right X, so the footer is retired once the export is done. */
      /* Remove the footer outright: the dialog closes from its top-right X, and a
         stylesheet rule for the footer would beat both [hidden] and a plain inline
         display:none. */
      dialog.querySelector("footer")?.remove();
    } finally { submit.disabled = false; }
  }

  function exportDialog() {
    const slug = studio.slug;
    const canvas = studio.timeline.canvas || {};
    /* The canvas carries the project's real pixel size; the ratio table is only
       a fallback so an unset canvas still reads as the default. */
    const canvasWidth = Number(canvas.width) || 0;
    const canvasHeight = Number(canvas.height) || 0;
    const size = canvasWidth && canvasHeight ? [canvasWidth, canvasHeight] : (ratios[canvas.ratio] || null);
    const fps = Number(studioFrameRate());
    // Export layout: read-only output facts, then one quality tier.
    // The canvas size and frame rate are not user-selectable here.
    /* The quality choice belongs to the project: preselect the saved one. */
    const qualityOf = (crf) => (Number(crf) === 18 ? "high" : Number(crf) === 28 ? "smaller" : "recommended");
    const savedQuality = String((studio.timeline && studio.timeline.export_quality) || (cur.doc && cur.doc.presets && cur.doc.presets.export_quality) || "recommended");
    const qualities = [[23, "Recommended", "Balanced clarity and file size"], [18, "High quality", "Higher bitrate · larger file"], [28, "Smaller file", "Lower bitrate · some detail may be lost"]]
      .map(([value, title, description]) => `<label><input name="quality" type="radio" value="${value}" ${String(qualityOf(value)) === savedQuality ? "checked" : ""}><span class="studio-export-quality-mark" aria-hidden="true"></span><span class="studio-export-quality-copy"><strong>${title}</strong><small>${description}</small></span></label>`).join("");
    const dialog = openDialog("Export video", `<fieldset class="studio-export-quality"><legend>Quality</legend>${qualities}</fieldset><p class="studio-export-status" role="status"></p>`, "Export video", (data, target) => exportVideo(data, target, slug))
    /* Exactly one tier is always selected.  An unrecognised stored value (an older
       client wrote "high" on every export) used to leave the group with nothing
       checked, and the fallback for that is Recommended. */
    const qualityInputs = [...dialog.querySelectorAll('input[name="quality"]')];
    if (qualityInputs.length && !qualityInputs.some((input) => input.checked)) {
      const fallback = qualityInputs.find((input) => qualityOf(Number(input.value)) === "recommended") || qualityInputs[0];
      fallback.checked = true;
    }
    /* The close control sits in the dialog's top-right corner, matching the other
       the dialogs, instead of only in the footer. */
    if (!dialog.querySelector(".studio-dialog-x")) {
      const topClose = document.createElement("button");
      topClose.type = "button";
      topClose.className = "studio-dialog-x";
      topClose.setAttribute("data-dialog-close", "");
      topClose.setAttribute("aria-label", "Close Export video");
      topClose.innerHTML = icon("close");
      topClose.onclick = () => dialog.close();
      dialog.append(topClose);
    };
    dialog.classList.add("studio-export-dialog");
    /* The dialog closes from the top-right X, so the footer keeps only the primary
       action instead of duplicating a close button at the bottom. */
    dialog.querySelector("footer button[data-dialog-close]")?.remove();
    return dialog;
  }

  function setDrawer(drawer) {
    panelState.drawer = drawer;
    const shell = document.querySelector(".studio-shell");
    if (!shell) return;
    shell.dataset.drawer = drawer || "";
    document.querySelectorAll("[data-studio-drawer]").forEach((button) => button.setAttribute("aria-expanded", String(button.dataset.studioDrawer === drawer)));
  }

  function enhance() {
    const shell = document.querySelector(".studio-shell");
    if (!shell || !studio.timeline) return;
    inspector();
    const header = document.querySelector(".reference-editor-top");
    const title = header?.querySelector("strong");
    if (title && !title.querySelector("button")) {
      const button = document.createElement("button");
      button.className = "studio-project-name";
      button.textContent = cur.doc.title || cur.slug;
      button.title = "Rename project";
      button.onclick = () => {
        const slug = studio.slug;
        openDialog("Rename project", `<label>Project name<input name="title" required maxlength="160" value="${escaped(cur.doc.title || slug)}"></label>`, "Save changes", async (data, dialog) => {
          await studio.persistQueue;
          if (studio.slug !== slug) throw new Error("The active project changed.");
          const latest = await jget(`./api/project/${encodeURIComponent(slug)}`);
          const name = String(data.get("title") || "").trim();
          if (!name) throw new Error("Enter a project name.");
          const result = await jpost(`./api/project/${encodeURIComponent(slug)}/save`, { base_rev: latest.project.rev, doc: { ...latest.project, title: name }, ops: [{ op: "project.rename", title: name }] });
          if (result.status >= 400) throw new Error(result.status === 409 ? "The project changed. Please retry." : "The name could not be saved.");
          cur.doc.title = name;
          cur.doc.rev = studio.rev = Number(result.json.rev);
          dialog.close();
          renderStudio();
        });
      };
      title.replaceChildren(button);
    }
    const topExport = header?.querySelector("[data-studio-export-top]");
    if (topExport) topExport.onclick = exportDialog;
    if (header && !header.querySelector(".studio-drawer-toggles")) {
      const drawers = document.createElement("div");
      drawers.className = "studio-drawer-toggles";
      drawers.innerHTML = '<button type="button" data-studio-drawer="media" aria-expanded="false">Media</button><button type="button" data-studio-drawer="inspector" aria-expanded="false">Properties</button>';
      drawers.querySelectorAll("button").forEach((button) => { button.onclick = () => setDrawer(panelState.drawer === button.dataset.studioDrawer ? null : button.dataset.studioDrawer); });
      header.insertBefore(drawers, header.querySelector(".spacer"));
    }
    setDrawer(panelState.drawer);
    const heading = shell.querySelector(".studio-stage .studio-panel-head h3");
    if (heading) heading.textContent = "Live preview";
    const bgLabel = shell.querySelector('label[for="studio-bg"]');
    if (bgLabel) bgLabel.textContent = "Background";
    const frameRate = shell.querySelector("#studio-fps");
    if (frameRate) frameRate.setAttribute("aria-label", "Frame rate");
    const previewButton = shell.querySelector("#studio-preview");
    if (previewButton) previewButton.textContent = "Render preview";
    const exportButton = shell.querySelector("#studio-export");
    if (exportButton) exportButton.textContent = "Export";
    shell.querySelectorAll('[data-bin-filter]').forEach((button) => {
      button.textContent = ({ all: "All", video: "Video", audio: "Audio" }[button.dataset.binFilter] || button.textContent);
    });
    const zoomLabel = shell.querySelector("#studio-zoom")?.previousElementSibling;
    if (zoomLabel?.classList.contains("muted")) zoomLabel.textContent = "Zoom";
    const status = shell.querySelector(".studio-job-status");
    if (status) status.textContent = status.textContent.replace("Source preview", "Source preview").replace("Final", "Final").replace("Preview queued", "Preview queued").replace("Composite preview ready", "Composite preview ready");
    const quality = shell.querySelector("#studio-exp-q");
    if (quality) [...quality.options].forEach((option) => { option.textContent = ({ "18": "High quality", "23": "Recommended", "28": "Fast" }[option.value] || option.textContent); });
    const ratio = document.querySelector("#studio-ratio");
    if (ratio) ratio.hidden = true;
    const transport = shell.querySelector(".studio-transport");
    const existingSettings = transport?.querySelector(".studio-canvas-settings");
    const existingMoreActions = shell.querySelector(".studio-toolbar-more .studio-more-actions");
    if (existingSettings && existingMoreActions && !existingMoreActions.contains(existingSettings)) {
      existingSettings.className = "studio-canvas-settings studio-more-action-button";
      existingSettings.innerHTML = `${icon("canvas")}<span class="canvas-label">Canvas</span> ${escaped(studio.timeline?.canvas?.ratio || "9:16")} ${icon("chevron")}`;
      transport.append(existingSettings);
    }
    if (transport && !transport.querySelector(".studio-playback-group")) {
      const group = document.createElement("div");
      group.className = "studio-playback-group";
      const time = transport.querySelector(".studio-time");
      const currentTime = time?.querySelector(".studio-current-time") || document.createElement("span");
      const totalTime = time?.querySelector(".studio-total-time") || document.createElement("span");
      currentTime.className = "studio-current-time";
      totalTime.className = "studio-total-time";
      if (!currentTime.textContent) currentTime.textContent = "00:00:00";
      if (!totalTime.textContent) totalTime.textContent = "00:00:00";
      const separator = document.createElement("span");
      separator.className = "studio-time-separator";
      separator.textContent = " / ";
      /* The transport carries time, play/pause and the total only;
         frame stepping lives on the arrow keys, not on extra buttons. */
      for (const [identifier, name] of [["studio-stop", "start"], ["studio-play", "play"]]) {
        const control = transport.querySelector(`#${identifier}`);
        if (!control) continue;
        if (name) control.innerHTML = icon(name);
        if (identifier === "studio-stop") { control.title = "Go to start"; control.setAttribute("aria-label", "Go to start"); }
        if (identifier === "studio-play") group.append(currentTime, control, separator, totalTime);
        else { control.hidden = false; group.append(control); }
      }
      const end = transport.querySelector("#studio-go-end") || document.createElement("button");
      end.type = "button"; end.id = "studio-go-end"; end.setAttribute("aria-label", "Go to end"); end.innerHTML = icon("end");
      end.onclick = () => { studioStopPlayback(false); studioSetPlayhead(studioContentDuration(), studioTotal()); };
      group.append(end);
      const loop = transport.querySelector("#studio-loop");
      if (loop) { loop.innerHTML = icon("loop"); loop.title = "Loop"; loop.hidden = false; group.append(loop); }
      const fps = transport.querySelector(".studio-fps");
      if (fps) fps.hidden = true;
      if (time) time.remove();
      transport.prepend(group);
      const settings = document.createElement("button");
      settings.type = "button"; settings.className = "studio-canvas-settings"; settings.innerHTML = `${icon("canvas")}<span class="canvas-label">Canvas</span> ${escaped(studio.timeline?.canvas?.ratio || "9:16")} ${icon("chevron")}`;
      settings.onclick = canvasDialog;
      transport.append(settings);
    }
    const playbackTime = transport?.querySelector(".studio-time");
    if (playbackTime && !playbackTime.querySelector(".studio-current-time")) {
      const raw = playbackTime.textContent || "";
      const parts = raw.split(" / ");
      playbackTime.innerHTML = `<span class="studio-current-time">${escaped(parts[0] || "00:00:00:00")}</span><span class="studio-time-separator"> / </span><span class="studio-total-time">${escaped(parts[1] || "00:00:00:00")}</span>`;
    }
    ["#studio-stop", "#studio-go-end", "#studio-loop"].forEach((selector) => transport?.querySelector(selector)?.removeAttribute("hidden"));
    const stageHeading = shell.querySelector(".studio-stage .studio-panel-head h3");
    if (stageHeading) stageHeading.hidden = true;
    const play = document.querySelector("#studio-play");
    if (play) {
      play.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (typeof global.studioTogglePlayback === "function") global.studioTogglePlayback();
      };
      play.innerHTML = studio.playing
        ? '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><rect x="7" y="5" width="3" height="14" rx="1"/><rect x="14" y="5" width="3" height="14" rx="1"/></svg>'
        : '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M8 5.8c0-.8.9-1.3 1.6-.9l10 6.2c.7.4.7 1.4 0 1.8l-10 6.2c-.7.4-1.6-.1-1.6-.9z"/></svg>';
      play.setAttribute("aria-label", studio.playing ? "Pause" : "Play");
    }
    const undo = shell.querySelector("#studio-undo");
    const redo = shell.querySelector("#studio-redo");
    if (undo) undo.innerHTML = `${icon("undo")}Undo`;
    if (redo) redo.innerHTML = `${icon("redo")}Redo`;
    const addText = shell.querySelector("#studio-add-caption");
    if (addText) {
      addText.innerHTML = `${icon("text")}Caption`;
      addText.title = "Add caption";
      /* The workspace layer uses data-workspace-mode (not the old
         data-workspace-tab name).  Keep this button as a real shortcut to
         the Text library, matching the Add text action. */
      addText.onclick = () => {
        const textTab = document.querySelector('[data-workspace-mode="text"]');
        if (textTab) textTab.click();
        else if (typeof global.workspaceStudioAddText === "function") global.workspaceStudioAddText("subtitle");
      };
    }
    const empty = shell.querySelector(".canvas-empty");
    if (empty) empty.innerHTML = 'No visual at this time<span>Add media or text to your timeline</span>';
    watchTimecode();
    decorateNumberInputs(document.querySelector(".studio-inspector"));
  }

  global.workspaceStudioPanels = enhance;
  /* The host renders its own clip inspector after the panels layer, so it
     calls this once the fields exist (and again on every tab switch) to wrap
     number inputs in always-visible steppers. */
  global.workspaceStudioDecorateInspector = () => decorateNumberInputs(document.querySelector(".studio-inspector"));
  /* Reference-parity "Add track" dialog: one hint line plus a track-type
     select, rendered with the same dialog chrome as the Canvas settings and
     Export dialogs. */
  global.workspaceStudioAddTrackDialog = (onAdd) => openDialog(
    "Add track",
    '<p class="studio-dialog-hint">Choose a track type, then drag media onto it.</p>'
      + '<label class="studio-dialog-field"><span>Track type</span>'
      + '<select name="type" aria-label="Track type">'
      + '<option value="video">Video / image</option><option value="audio">Audio</option>'
      + '<option value="text">Dialogue subtitles</option><option value="title">Titles &amp; styled text</option>'
      + '</select></label>',
    "Add track",
    (data, dialog) => {
      if (typeof onAdd === "function") onAdd(String(data?.get?.("type") || "video"));
      /* Confirm closes the dialog, exactly like Canvas settings and Export. */
      dialog?.close();
    },
  );
  global.workspaceStudioEditText = () => editTextDialog();
  /* Which inspector tab is showing (the timeline uses it for its junction ring). */
  global.workspaceStudioInspectorTab = () => panelState.tab;
  global.workspaceStudioShowInspector = (tab) => { panelState.selection = studioSelected()?.item.id || null; panelState.slug = studio.slug; panelState.tab = tab; inspector(); setDrawer("inspector"); };
  global.workspaceStudioCanvasDialog = canvasDialog;
  global.workspaceStudioExportDialog = exportDialog;
  global.workspaceStudioTransport = () => {
    const play = document.querySelector("#studio-play");
    if (play) { play.innerHTML = icon(studio.playing ? "pause" : "play"); play.setAttribute("aria-label", studio.playing ? "Pause" : "Play"); }
  };
})(window);


