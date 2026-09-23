/* Global Projects / Assets workspace surface.
 * Uses the manager's real API and project-relative media URLs; no mock state.
 */
(function (global) {
  "use strict";
  const state = { query: "", origin: "all", sort: "updated", payload: null };
  const text = (v) => String(v == null ? "" : v);
  const escape = (v) => text(v).replace(/[&<>\"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[c]));
  const encodePath = (v) => encodeURIComponent(text(v));
  function mediaUrl(item) {
    const slug = text(item?.project_slug); const file = text(item?.file).replace(/\\/g, "/");
    if (!slug || !file || file.split("/").some((p) => !p || p === "." || p === ".." || /[:]/.test(p))) return "";
    return `./${encodePath(slug)}/${file.split("/").map(encodePath).join("/")}`;
  }
  function notify(message) { if (typeof global.toast === "function") global.toast(message); }
  // Project cards open the real editing surface directly.  The detail tabs
  // remain available from the editor's project menu/back link.
  function projectHref(slug) { return `#/p/${encodeURIComponent(text(slug))}/studio`; }
  function formatDate(v) { return localTimeText(v, { seconds: false }).text.slice(0, 10) || "—"; }
  function formatDuration(v) {
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return "—";
    const total = Math.max(0, Math.round(n));
    return [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60].map((part) => String(part).padStart(2, "0")).join(":");
  }
  function originLabel(v) { return v === "generated" ? "AI generated" : v === "upload" ? "Uploaded" : v === "export" ? "Export" : "Derived"; }

  /* Timestamps are stored as UTC.  Show the reader's local time and keep the raw
     value for tooltips; the sort order still uses the raw strings. */
  function localTimeText(value, options) {
    const seconds = !options || options.seconds !== false;
    const when = new Date(String(value || ""));
    if (isNaN(when.getTime())) return { text: String(value || ""), utc: String(value || "") };
    const pad = (n) => String(n).padStart(2, "0");
    const day = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
    const clock = seconds
      ? `${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`
      : `${pad(when.getHours())}:${pad(when.getMinutes())}`;
    return { text: `${day} ${clock}`, utc: String(value || "") };
  }
  function projectCover(project, assets) {
    const row = assets.find((x) => x.project_slug === project.slug && x.poster) || assets.find((x) => x.project_slug === project.slug && x.kind === "image");
    return row ? mediaUrl({ project_slug: row.project_slug, file: row.poster || row.file }) : "";
  }
  function isNameSort() { return state.sort === "name" || state.sort === "title"; }
  function projectCards(projects, assets) {
    const query = state.query.trim().toLowerCase();
    const rows = projects.filter((p) => !query || `${p.title || ""} ${p.slug || ""}`.toLowerCase().includes(query));
    const sorted = [...rows].sort((a, b) => isNameSort() ? text(a.title).localeCompare(text(b.title), "zh") : text(b.updated).localeCompare(text(a.updated)));
    return sorted.map((p) => {
      const cover = projectCover(p, assets);
      return `<a class="card" href="${projectHref(p.slug)}" aria-label="Open ${escape(p.title || p.slug)}"><div class="thumb"${cover ? ` style="background-image:url('${escape(cover)}');background-size:cover;background-position:center"` : ""}>${cover ? "" : "◈"}<b>${escape(p.ratio || "16:9")}</b></div><div class="card-body"><div class="title">${escape(p.title || p.slug)}</div><div class="row"><span class="dot"></span>${Number(p.clips_done || 0)}/${Number(p.clips_total || 0)} clips · ${formatDate(p.updated)}</div></div></a>`;
    }).join("");
  }
  function assetCards(assets) {
    const query = state.query.trim().toLowerCase();
    const rows = assets.filter((a) => a.kind === "video")/* ALL means every reusable item: generations, uploads and exports alike. */
      .filter((a) => state.origin === "all" ? true : a.origin === state.origin).filter((a) => !query || `${a.name || ""} ${a.project_title || ""} ${a.kind || ""}`.toLowerCase().includes(query));
    const sorted = [...rows].sort((a, b) => state.sort === "name" ? text(a.name).localeCompare(text(b.name), "zh") : text(b.created).localeCompare(text(a.created)));
    return sorted.map((a) => { const url = mediaUrl(a), poster = a.poster ? mediaUrl({ project_slug: a.project_slug, file: a.poster }) : ""; return `<button class="wx-asset-card" type="button" data-wx-project="${escape(a.project_slug)}" data-wx-asset="${escape(a.id)}"><div class="wx-asset-thumb"${poster ? ` style="background-image:url('${escape(poster)}')` : ""}>${poster ? "" : "▶"}</div><div class="wx-asset-name">${escape(a.name || a.id)}</div><div class="wx-asset-meta"><span class="wx-origin ${a.origin === "generated" ? "generated" : ""}">${originLabel(a.origin)}</span><span>${escape(a.project_title || a.project_slug)}</span></div><span class="wx-asset-hidden-url" data-url="${escape(url)}"></span></button>`; }).join("");
  }  function controls(mode) { return `<div class="wx-tools"><label class="wx-search"><span aria-hidden="true">⌕</span><input id="wx-query" type="search" value="${escape(state.query)}" placeholder="Search projects and assets" aria-label="Search projects and assets"></label>${mode === "assets" ? `<select id="wx-origin" aria-label="Asset origin"><option value="all" ${state.origin === "all" ? "selected" : ""}>All assets</option><option value="generated" ${state.origin === "generated" ? "selected" : ""}>AI generated</option><option value="upload" ${state.origin === "upload" ? "selected" : ""}>Uploaded</option></select>` : ""}<select id="wx-sort" aria-label="Sort"><option value="updated" ${state.sort === "updated" ? "selected" : ""}>Recently updated</option><option value="name" ${state.sort === "name" ? "selected" : ""}>Name</option></select>${mode === "projects" ? `<button type="button" class="primary" id="wx-new">＋ New project</button>` : ""}</div>`; }
  function bindControls(render) { const q = document.querySelector("#wx-query"); if (q) { q.oninput = () => { state.query = q.value; render(); }; q.onkeydown = (e) => { if (e.key === "Escape") { q.value = ""; state.query = ""; render(); } }; } const sort = document.querySelector("#wx-sort"); if (sort) sort.onchange = () => { state.sort = sort.value; render(); }; const origin = document.querySelector("#wx-origin"); if (origin) origin.onchange = () => { state.origin = origin.value; render(); }; const create = document.querySelector("#wx-new"); if (create) create.onclick = createProject; }
  async function createProject() {
    /* Reference parity: the dialog asks for the name (prefilled) and the aspect
       ratio, and the new project takes that ratio. */
    const details = typeof global.askVideoProject === "function"
      ? await global.askVideoProject("Untitled video")
      : (typeof global.askText === "function" ? await global.askText("New project", "Untitled video", "Project name") : null);
    if (!details) return;
    const title = text(typeof details === "string" ? details : details.title).trim();
    const ratio = typeof details === "object" ? String(details.ratio || "16:9") : "16:9";
    if (!title || typeof global.jpost !== "function") return;
    const result = await global.jpost("./api/projects/create", { title, type: "generate" });
    if (result?.status !== 200 || !result.json?.slug) return notify("Project creation failed");
    const slug = result.json.slug;
    /* The create endpoint takes a title only, so the chosen ratio is written to
       the fresh project's timeline with the same op the canvas dialog uses. */
    const sizes = { "16:9": [1920, 1080], "9:16": [1080, 1920], "1:1": [1080, 1080], "4:5": [1080, 1350] };
    const size = sizes[ratio];
    if (size && typeof global.jget === "function") {
      try {
        const doc = await global.jget(`./api/project/${encodeURIComponent(slug)}`);
        const rev = doc?.rev ?? doc?.project?.rev ?? 0;
        const timeline = doc?.project?.assembly?.timeline || {};
        timeline.canvas = { ...(timeline.canvas || {}), ratio, width: size[0], height: size[1] };
        await global.jpost(`./api/project/${encodeURIComponent(slug)}/timeline/commit`, { base_rev: rev, operations: [{ op: "replace_timeline", timeline }] });
      } catch (_) { /* the project still opens with its default ratio */ }
    }
    notify("Project created");
    location.hash = projectHref(slug);
  }
  /* The chips are rebuilt with the shell, so every path that renders the shell
     has to bind them; before this only the #/assets route did, and the default
     view's chips were inert. */
  function bindOriginFilters(root, assets) {
    if (!root) return;
    const chips = [...root.querySelectorAll("[data-origin-filter]")];
    chips.forEach((button) => {
      button.onclick = () => {
        state.origin = button.dataset.originFilter;
        chips.forEach((other) => other.classList.toggle("active", other === button));
        renderAssetsInto(root.querySelector("#assetGrid"), assets);
      };
    });
    chips.forEach((button) => button.classList.toggle("active", button.dataset.originFilter === state.origin));
  }
  function projectView(payload) {
    const projects = Array.isArray(payload?.projects) ? payload.projects : [];
    const assets = Array.isArray(payload?.assets) ? payload.assets : [];
    const recent = [...projects].sort((a, b) => text(b.updated).localeCompare(text(a.updated)))[0];
    const cover = recent ? projectCover(recent, assets) : "";
    document.body.classList.remove("studio-mode");
    document.body.classList.add("reference-home-mode");
    const legacyTop = document.querySelector("body > .top");
    if (legacyTop) legacyTop.style.display = "none";
    document.querySelector("#crumb").textContent = "";
    const app = document.querySelector("#app");
    app.innerHTML = `<div class="app reference-home" id="referenceHome">
      <header class="topbar"><nav class="nav"><button class="active" data-view="projects">Projects</button><button data-view="assets">Assets</button><button data-view="logs">Logs</button></nav><div class="grow"></div><button class="primary" id="newProject">+ New project</button></header>
      <main id="projectsView"><section class="hero"><div><div class="eyebrow">Video workspace</div></div></section>
        ${recent ? `<article class="continue" id="continueProject"><div class="continue-visual"><div class="cover-perspective">${cover ? `<img class="photo-cover" src="${escape(cover)}" alt="">` : ""}<span class="cover-tag">LAST EDITED</span><span class="play">▶</span></div></div><div class="continue-copy"><span class="badge">Recent project</span><h3>${escape(recent.title || recent.slug)}</h3><div class="meta">${Number(recent.clips_done || 0)} clips · ${escape(recent.ratio || "16:9")}</div><a class="primary" href="${projectHref(recent.slug)}">Continue editing →</a></div></article>` : ""}
        <div class="section-head"><h2>All projects</h2><span id="projectCount">${projects.length}</span><button class="link" id="sortProjects" aria-label="Sort projects by ${isNameSort() ? "name" : "last updated"}">Sort: ${isNameSort() ? "Name" : "Updated"}</button></div>
        <div class="grid" id="projectGrid">${projectCards(projects, assets) || `<div class="empty"><strong>No projects yet.</strong><span>Start a video task and it will appear here.</span></div>`}</div>
      </main>
      <main id="assetsView" class="hidden"><section class="hero"><div><div class="eyebrow">Reusable media</div><h1>All your assets, ready to build with.</h1><div class="sub">Find your generations, uploads, and exports here. Add them to any project when you need them.</div></div><button class="secondary" id="uploadWorkspace">↑ Upload media</button></section><div class="filters"><button class="filter active" data-origin-filter="all">All</button><button class="filter" data-origin-filter="generated">Generated</button><button class="filter" data-origin-filter="upload">Uploads</button><button class="filter" data-origin-filter="export">Exports</button></div><div class="asset-grid" id="assetGrid"></div></main>
    </div>`;
    const home = app.querySelector("#referenceHome");
    const render = () => projectView(state.payload);
    home.querySelectorAll("[data-view]").forEach((button) => button.onclick = () => { location.hash = button.dataset.view === "projects" ? "#/" : (button.dataset.view === "assets" ? "#/assets" : "#/logs"); });
    home.querySelector("#newProject")?.addEventListener("click", createProject);
    home.querySelector("#sortProjects")?.addEventListener("click", () => { state.sort = isNameSort() ? "updated" : "name"; projectView(state.payload); });
    home.querySelector("#continueProject")?.addEventListener("click", (event) => { if (!event.target.closest("a")) location.hash = projectHref(recent.slug).slice(1); });
    home.querySelectorAll("[data-wx-asset]").forEach((button) => { const item = assets.find((asset) => String(asset.id) === String(button.dataset.wxAsset) && String(asset.project_slug || "") === String(button.dataset.wxProject || "")); if (item) button.onclick = () => assetModal(item); });
    bindControls(render);
    bindOriginFilters(home, assets);
    renderAssetsInto(home.querySelector("#assetGrid"), assets);
    home.querySelector("#uploadWorkspace")?.addEventListener("click", () => global.uploadWorkspaceVideo());
    /* Warm the project the user is most likely to open next, so its first click
       is instant instead of waiting a full round trip. */
    if (typeof global.warmProject === "function" && recent && recent.slug) {
      const idle = global.requestIdleCallback || ((fn) => setTimeout(fn, 400));
      idle(() => global.warmProject(recent.slug));
    }
  }
  function renderAssetsInto(target, assets) {
    if (!target) return;
    const query = state.query.trim().toLowerCase();
    const rows = assets.filter((a) => a.kind === "video")/* ALL means every reusable item: generations, uploads and exports alike. */
      .filter((a) => state.origin === "all" ? true : a.origin === state.origin).filter((a) => !query || `${a.name || ""} ${a.project_title || ""} ${a.kind || ""}`.toLowerCase().includes(query));
    target.innerHTML = rows.map((item) => { const poster = item.poster ? mediaUrl({ project_slug: item.project_slug, file: item.poster }) : ""; const isExport = item.origin === "export";
      const meta = isExport ? `${escape(item.preset || "Studio export")} · ${escape(item.project_title || item.project_slug || "")} · ${escape(localTimeText(item.created, { seconds: false }).text)}` : `${originLabel(item.origin)} · ${escape(item.project_title || item.project_slug || "")}`;
      const dl = isExport ? `<div class="asset-actions"><a class="asset-download" href="./${encodeURIComponent(item.project_slug)}/${encodeURI(String(item.file || ""))}" download aria-label="Download ${escape(item.name || "video")}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11m0 0 4-4m-4 4-4-4"/><path d="M5 19h14"/></svg><span>Download</span></a></div>` : "";
      return `<article class="asset" role="button" tabindex="0" aria-label="Open ${escape(item.name || item.id)}" data-wx-project="${escape(item.project_slug)}" data-wx-asset="${escape(item.id)}"><div class="asset-thumb"${poster ? ` style="background-image:url('${escape(poster)}')"` : ""}><span class="duration">${escape(formatDuration(item.duration))}</span></div><div class="asset-name">${escape(item.name || item.id)}</div><div class="asset-meta">${meta}</div>${dl}</article>`; }).join("") || (state.origin === "export" ? '<div class="empty">No exports yet. Export from the Studio and it appears here.</div>' : '<div class="empty">No videos here. Upload a video or change the filter.</div>');
    /* The card itself opens the detail modal, so a download click must not bubble
       into it (otherwise the file saves and the modal opens at the same time). */
    target.querySelectorAll(".asset-download").forEach((node) => { node.addEventListener("click", (event) => event.stopPropagation()); });
    target.querySelectorAll("[data-wx-asset]").forEach((node) => { const item = rows.find((asset) => String(asset.id) === node.dataset.wxAsset && asset.project_slug === node.dataset.wxProject); if (item) { node.onclick = () => assetModal(item); node.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); assetModal(item); } }; } });
  }
  /* The library payload is the most expensive response the manager produces, and
     page switches used to refetch it every time (measured 940 ms locally).  Keep
     it, paint it at once, and only repaint when the index token moved. */
  let libraryPayload = null;
  let libraryToken = "";
  let renderedToken = null;
  /* The router reports cache hits, so it needs to know whether this surface can
     paint without waiting for the network. */
  global.workspaceHasLibraryPayload = () => Boolean(libraryPayload);
  /* One fetch path for every surface: it sends the revision it already holds, so
     an unchanged library costs a tiny 304 instead of the whole payload. */
  async function fetchLibrary() {
    try {
      const headers = libraryToken ? { "If-None-Match": '"' + libraryToken + '"' } : {};
      const response = await fetch("./api/library", { cache: "no-store", headers });
      if (response.status === 304) return { ok: true, changed: false, payload: libraryPayload };
      if (!response.ok) throw new Error(String(response.status));
      const payload = await response.json();
      const token = String(payload?.index_token || "");
      const changed = token !== libraryToken;
      libraryPayload = payload; libraryToken = token; state.payload = payload;
      return { ok: true, changed, payload };
    } catch (error) {
      return { ok: false, changed: false, payload: libraryPayload, error };
    }
  }
  async function refreshLibrary(silent) {
    const result = await fetchLibrary();
    if (!result.ok) {
      if (silent) return;   /* keep the view the user already has */
      document.querySelector("#app").innerHTML = `<div class="wx-empty"><strong>Unable to load the asset library</strong><span>The manager will retry; refresh later.</span></div>`;
      return;
    }
    if (!silent || result.changed) { libraryView(result.payload); renderedToken = libraryToken; }
  }
  async function loadLibrary() {
    if (libraryPayload) {
      state.payload = libraryPayload;
      /* A cached payload always repaints.  Skipping the repaint when "nothing
         changed" was tried and removed: it saved only the DOM rebuild (2-12 ms)
         while the paint cost stayed the same, and guessing when the on-screen
         shell still matched produced two real regressions (Projects and Assets
         showing the same shell, and an empty page after Back from a project). */
      libraryView(libraryPayload);
      renderedToken = libraryToken;
      refreshLibrary(true);
      return;
    }
    await refreshLibrary(false);
  }
  function chooseExistingProject(item) {
    const projects = Array.isArray(state.payload?.projects) ? state.payload.projects : [];
    if (!projects.length) return notify("No projects available. Create a project first.");
    if (typeof global.showModal !== "function") return;
    global.showModal(`<h3 id="modal-title">Add to existing project</h3><p class="muted" style="margin:8px 0 12px">Choose a project. The video will be copied into its asset library.</p><div class="choice-list">${projects.map((p) => `<button type="button" class="secondary" data-wx-pick="${escape(p.slug)}">${escape(p.title || p.slug)}</button>`).join("")}</div>`);
    document.querySelectorAll("[data-wx-pick]").forEach((button) => { button.onclick = async () => { const slug = button.dataset.wxPick; let baseRev = 0; try { const target = await global.jget(`./api/project/${encodeURIComponent(slug)}`); baseRev = Number(target?.project?.rev || 0); } catch (_) {} const result = await global.jpost(`./api/project/${encodeURIComponent(slug)}/import-asset`, { source_project: item.project_slug, asset_id: item._apiId || item.id, base_rev: baseRev }); if (result?.status === 200 && result.json?.ok) { global.hideModal?.(); notify("Video added to project"); } else notify(result?.json?.error || "Asset import failed"); }; });
  }
  function assetModal(item) {
    const url = mediaUrl(item), poster = item.poster ? mediaUrl({ project_slug: item.project_slug, file: item.poster }) : "";
    if (typeof global.openVideoAssetModal === "function") return global.openVideoAssetModal({ ...item, _apiId: item.id, _url: url, _poster: poster });
  }
  async function importAsset(item) { let project = null; try { project = typeof cur !== "undefined" ? cur : null; } catch (_) { project = null; } const slug = project?.slug || ""; if (!slug) { notify("Open a project first."); return; } const baseRev = Number(project?.doc?.rev || 0); const result = typeof global.jpost === "function" ? await global.jpost(`./api/project/${encodeURIComponent(slug)}/import-asset`, { source_project: item.project_slug, asset_id: item.id, base_rev: baseRev }) : null; if (result?.status === 200 && result.json?.ok) { if (typeof global.hideModal === "function") global.hideModal(); notify(result.json.reused ? "Asset already in project" : "Asset added to project"); if (location.hash.includes(`/p/${encodeURIComponent(slug)}/`)) { project.doc = result.json.project; if (typeof global.route === "function") global.route(); } } else notify(result?.json?.error || "Asset import failed"); }
  async function createProjectWithAsset(item) { const details = typeof global.askVideoProject === "function" ? await global.askVideoProject("") : { title: "Untitled project", ratio: "16:9" }; if (!details || !text(details.title).trim() || typeof global.jpost !== "function") return; const created = await global.jpost("./api/projects/create", { title: text(details.title).trim(), type: "generate" }); if (created?.status !== 200 || !created.json?.slug) return notify("Project creation failed"); const target = created.json.slug; const imported = await global.jpost(`./api/project/${encodeURIComponent(target)}/import-asset`, { source_project: item.project_slug, asset_id: item.id, base_rev: Number(created.json.rev || 0) }); if (imported?.status === 200 && imported.json?.ok) { if (typeof global.hideModal === "function") global.hideModal(); notify("Project created and asset added"); location.hash = projectHref(target); } else notify(imported?.json?.error || "Asset import failed"); }
  function libraryView(payload) {
    const assets = Array.isArray(payload?.assets) ? payload.assets : [], projects = Array.isArray(payload?.projects) ? payload.projects : [];
    document.body.classList.remove("studio-mode"); document.body.classList.add("reference-home-mode");
    const legacyTop = document.querySelector("body > .top"); if (legacyTop) legacyTop.style.display = "none";
    document.querySelector("#crumb").textContent = ""; const app = document.querySelector("#app");
    app.innerHTML = `<div class="app reference-home" id="referenceHome"><header class="topbar"><nav class="nav"><button data-view="projects">Projects</button><button class="active" data-view="assets">Assets</button><button data-view="logs">Logs</button></nav><div class="grow"></div><button class="primary" id="newProject">+ New project</button></header><main id="projectsView" class="hidden"></main><main id="assetsView"><section class="hero"><div><div class="eyebrow">Reusable media</div><h1>All your assets, ready to build with.</h1><div class="sub">Find your generations, uploads, and exports here. Add them to any project when you need them.</div></div><button class="secondary" id="uploadWorkspace">↑ Upload media</button></section><div class="filters"><button class="filter active" data-origin-filter="all">All</button><button class="filter" data-origin-filter="generated">Generated</button><button class="filter" data-origin-filter="upload">Uploads</button><button class="filter" data-origin-filter="export">Exports</button></div><div class="asset-grid" id="assetGrid"></div></main></div>`;
    const home = app.querySelector("#referenceHome");
    home.querySelectorAll("[data-view]").forEach((button) => button.onclick = () => { location.hash = button.dataset.view === "projects" ? "#/" : (button.dataset.view === "assets" ? "#/assets" : "#/logs"); });
    home.querySelector("#newProject")?.addEventListener("click", createProject); home.querySelector("#uploadWorkspace")?.addEventListener("click", () => global.uploadWorkspaceVideo());
    bindOriginFilters(home, assets);
    renderAssetsInto(home.querySelector("#assetGrid"), assets);
  }

  /* Home "Logs" view: the runtime records every operation in each project's
     logs/edits.log; this surface lists the newest entries across projects. */
  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }

  async function workspaceShowLogsView() {
    const app = document.querySelector("#app");
    /* A hash change (or a cold load on #/logs) rebuilds the home shell
       asynchronously, which discarded the view we just injected.  Arm the
       observer first, before any early return, so the view is re-mounted
       whenever the shell appears. */
    if (!workspaceShowLogsView.__armed) {
      workspaceShowLogsView.__armed = true;
      if (app && typeof MutationObserver === "function") {
        new MutationObserver(() => {
          if (!location.hash.startsWith("#/logs")) return;
          if (!document.getElementById("logsView")) workspaceShowLogsView();
        }).observe(app, { childList: true });
      }
    }
    const home = app && app.querySelector("#referenceHome");
    if (!home) return;
    for (const id of ["projectsView", "assetsView"]) {
      const other = home.querySelector("#" + id);
      if (other) { other.classList.add("hidden"); other.style.display = "none"; }
    }
    home.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === "logs"));
    /* Keep the log surface in the persistent #app container: the home shell is
       rebuilt on every route change, and rebuilding this view is what made
       switching to Logs flash. */
    /* Keep the log surface inside the shell: a body-level, full-screen overlay
       covered the app header, so Export and every other navigation control
       stopped responding while Logs was open. */
    let view = home.querySelector("#logsView");
    if (!view) {
      view = document.createElement("main");
      view.id = "logsView";
      if (workspaceShowLogsView.__cache) view.innerHTML = workspaceShowLogsView.__cache;
      home.appendChild(view);
    }
    view.classList.remove("hidden");
    if (state.logNoise !== true) state.logNoise = false;
    view.innerHTML = `<section class="hero"><div><div class="eyebrow">Activity</div><h1>Operation log</h1>
      <div class="sub">Every recorded operation, newest first, in your local time (UTC shown on hover). Consecutive identical operations are grouped; each entry comes from a project's logs/edits.log.</div></div></section>
      <div class="log-bar"><h2>Recent operations</h2><span id="logCount" class="log-counter">…</span>
        <button type="button" id="logNoiseToggle" class="link">${state.logNoise ? "Showing machine noise" : "Hiding machine noise"}</button></div>
      <div id="logList"><div class="empty"><strong>Loading…</strong></div></div>`;
    const list = view.querySelector("#logList");
    const counter = view.querySelector("#logCount");
    workspaceShowLogsView.__cache = view.innerHTML;
    try {
      const response = await fetch("./api/logs?limit=200", { cache: "no-store" });
      const payload = await response.json();
      const entries = Array.isArray(payload.entries) ? payload.entries : [];
      counter.textContent = `${entries.length} entr${entries.length === 1 ? "y" : "ies"}`;
      if (!entries.length) {
        list.innerHTML = `<div class="empty"><strong>No operations recorded yet.</strong><span>Edits, uploads, renders and exports appear here.</span></div>`;
        return;
      }
      /* Presentation-only cleanup: preview renders and pointer moves are noise for
         a human reader, and repeated property writes collapse into one line with
         a count.  Nothing is removed from the underlying log. */
      const NOISE_OPS = new Set(["render.completed", "move", "cosmetic", "assembly.preview_built", "replace_timeline"]);
      /* Friendly titles for the Operation column; the raw op stays in the row
         tooltip so the log is still readable for debugging. */
      const OP_TITLES = {
        "set_caption": "Caption changed", "caption.add": "Caption added", "caption.remove": "Caption removed",
        "set_transition": "Transition changed", "set_transform": "Position or scale changed",
        "set_track": "Track changed", "replace_timeline": "Timeline saved",
        "asset.upload": "Media uploaded", "asset.import": "Media imported", "asset.prepare": "Media prepared",
        "add": "Item added", "remove": "Item removed", "move": "Item moved", "split": "Clip split",
        "trim": "Clip trimmed", "clip.trim": "Clip trimmed", "clip.revision_requested": "Revision requested",
        "export.created": "Video exported", "render.completed": "Preview rendered",
        "assembly.reorder": "Assembly order changed", "assembly.preview_built": "Assembly preview built",
        "project.created": "Project created", "project.updated": "Project updated",
        "intent.message": "Draft message",
      };
      const opTitle = (op) => OP_TITLES[op] || String(op || "operation");
      /* The log stores UTC (…Z).  Show the reader's local time and keep the raw
         UTC stamp available in the row tooltip. */
      const localStamp = (value) => localTimeText(value);
      const visible = state.logNoise ? entries : entries.filter((row) => !NOISE_OPS.has(String(row.op || "")));
      const groups = [];
      for (const row of visible) {
        const key = `${row.op || ""}|${row.project || ""}`;
        const stamp = Date.parse(row.ts || "") || 0;
        const last = groups[groups.length - 1];
        if (last && last.key === key && stamp && last.lastStamp && Math.abs(last.lastStamp - stamp) <= 60000) {
          last.count += 1; last.lastStamp = stamp; last.ts = row.ts; last.rev = row.rev; continue;
        }
        const detail = Object.entries(row)
          .filter(([key2]) => !["op", "ts", "rev", "project"].includes(key2))
          .map(([key2, value]) => `${key2}: ${Array.isArray(value) ? value.join(", ") : value}`)
          .join(" · ");
        groups.push({ key, op: row.op, project: row.project, rev: row.rev, ts: row.ts, firstTs: row.ts,
                      lastStamp: stamp, count: 1, detail });
      }
      const hiddenNote = state.logNoise ? "" : ` · ${entries.length - visible.length} preview/move entries hidden`;
      counter.textContent = `${groups.length} lines from ${visible.length} operations${hiddenNote}`;
      list.innerHTML = `<div class="log-line log-head"><span>Time</span><span>Operation</span><span>Project</span><span>Rev</span><span>Detail</span></div>`
        + groups.map((group) => {
          const title = opTitle(group.op);
          const detail = group.detail || "";
          const tip = group.count > 1
            ? `${group.op} · grouped ${group.count} identical operations: ${group.firstTs} → ${group.ts}`
            : `${group.op}${detail ? " · " + detail : ""}`;
          const stamp = localStamp(group.ts);
          const tipFull = `${tip}${stamp.utc ? ` · UTC ${stamp.utc}` : ""}`;
          return `<div class="log-line" title="${escapeHtml(tipFull)}">
            <span class="log-ts">${escapeHtml(stamp.text)}</span>
            <span class="log-op">${escapeHtml(title)}${group.count > 1 ? `<em class="log-count">×${group.count}</em>` : ""}</span>
            <span class="log-project">${escapeHtml(group.project || "")}</span>
            <span class="log-rev">${group.rev != null ? escapeHtml(group.rev) : ""}</span>
            <span class="log-detail">${escapeHtml(detail)}</span>
          </div>`;
        }).join("");
      view.querySelector("#logNoiseToggle").onclick = () => { state.logNoise = !state.logNoise; workspaceShowLogsView(); };
    } catch (error) {
      counter.textContent = "";
      list.innerHTML = `<div class="empty"><strong>The log could not be loaded.</strong><span>${escapeHtml(error && error.message ? error.message : "request failed")}</span></div>`;
    }
    workspaceShowLogsView.__cache = view.innerHTML;
  }

  /* The projects surface (the Back destination) shares the same payload and the
     same "only repaint when something moved" rule: rebuilding the whole home shell
     on every Back press was the single most expensive thing the UI did. */
  async function renderProjects(payload) {
    if (payload) { state.payload = payload; projectView(payload); renderedToken = libraryToken; return; }
    if (libraryPayload) {
      state.payload = libraryPayload;
      projectView(libraryPayload);
      renderedToken = libraryToken;
      const result = await fetchLibrary();          /* background revalidate */
      if (result.ok && result.changed) {
        projectView(result.payload);
        renderedToken = libraryToken;
      }
      return;
    }
    const first = await fetchLibrary();
    const data = first.payload || { projects: [], assets: [] };
    state.payload = data;
    projectView(data);
    renderedToken = libraryToken;
  }
  /* A landed export or upload updates the library without hijacking the page.
     `loadLibrary` rebuilds the whole home shell, so calling it from the render
     completion replaced the Studio with the assets page while the URL still said
     /studio, and the origin filter kept the chip the user had touched last.  Here
     the payload is refetched, the filter points at the new item, and the grid is
     only repainted when the assets view is what the user is looking at. */
  async function focusLibraryOrigin(origin) {
    if (origin) state.origin = String(origin);
    try {
      const response = await fetch("./api/library", { cache: "no-store" });
      if (response.ok) state.payload = await response.json();
    } catch (_) { /* the view keeps its last payload */ }
    const grid = document.querySelector("#assetGrid");
    if (!grid) return false;
    bindOriginFilters(grid.closest("#referenceHome") || document, (state.payload && state.payload.assets) || []);
    renderAssetsInto(grid, (state.payload && state.payload.assets) || []);
    return true;
  }
  global.workspaceLibraryFocus = focusLibraryOrigin;
  global.workspaceRenderProjects = renderProjects;
  global.workspaceRenderLibrary = loadLibrary;
  global.workspaceRefreshLibrary = loadLibrary;
  global.workspaceShowLogsView = workspaceShowLogsView;
})(window);


