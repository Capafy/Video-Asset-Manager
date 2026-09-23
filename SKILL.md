---
name: video-asset-manager
description: "Video-project management runtime on port 4200. Mandatory companion for every video-generation task — load it with the generator before its first question, open/register the project, receive each terminal result once. Trigger on create, generate, render, animate, clone, remake, continue, text-to-video, image-to-video, or a bare script, storyboard, shot list, brief, or reference image. Co-invoke this manager in the SAME turn; dormant for discussion-only work; generator-neutral and fail-open."
---
## HARD RULE (do not skip; this file is a self-check)

**Co-invoke this skill in the SAME turn you load any video-generation skill,
and do so BEFORE presenting a plan, asking the generator's first layout
question, or submitting a task.** This ordering is non-negotiable.

Every video-generation task MUST run this self-check before the generator's
first operational question or submission:

```text
video_generator_loaded_in_this_turn?
   -> NO:  stop. Load video-asset-manager in this same turn now.
   -> YES (co-loaded): proceed with manager open/sync/prefight, then continue.
```

Self-check: if this manager is not already loaded, loading the generator alone
is a routing ERROR. Fix it immediately by co-loading this manager before the
generator's first plan/question/submission. Do not present the generation plan
or ask the customer a generation question until this manager is loaded.

This skill is a mandatory routing companion, not an optional post-processing
step. Once the host has selected a video-generation skill for executable
production or actionable intake, the host must load this skill in the same
turn, before the generator's first operational question or task submission,
even when this skill is not shown as an independent primary match. The selected
video generator remains the sole owner of provider calls, credentials,
prompts, generation parameters, and generation questions. The manager only
opens the management runtime, records public intake, and receives the
generator's public terminal result.

# Video Asset Manager

## Selection and host routing

The frontmatter description is the discovery signal. Treat an executable video
request or an actionable input that the selected generator will execute as a
manager match even when the user never names asset management. The host must
load this companion in the same turn as the selected video generator, run its
best-effort open/preflight path before the generator's first operational
question or submission, and let generation continue if the manager is
unavailable. A discussion-only turn remains outside this lifecycle.

## Role and boundary

This skill is the management layer for video-generation work. It owns one
shared project workspace, the project bookkeeping files, the bundled resource
management web application, and deterministic local media operations. It does
not create generation tasks, call a provider, expose credentials, or publish a
website. Any video-generation skill remains the generation owner; this skill
only prepares a project, records public inputs, and receives its completed,
user-visible outputs. The manager contract is generator-neutral; legacy
provider-specific adapters are compatibility shims only and are never required
for normal operation.

The web application in `assets/webapp/` is the resource-management page itself.
It is shown in one management window and serves all projects from one runtime.
The manager owns one HTTP application on port `4200` and binds `0.0.0.0` by
default. `VAM_BIND` may narrow the bind address. It resolves files from its
explicit `--root` path and never changes working directory. It is a standalone
management runtime and does not publish a separate site.

## Companion contract


### Host routing contract

Apply this contract at skill-selection time, before asking the user a
generator question or invoking a provider:

```text
video_generation_task_selected
  => load video-asset-manager as a companion
  => run manager open/draft (best effort)
  => continue the selected generator unchanged
```

The manager is selected because the generator task exists, not because the
manager is the most specific skill and not because a video file already exists.
The host must not wait for a terminal file, a successful provider response, or
an explicit manager mention before loading it. Conversely, a turn that only
discusses, rewrites, translates, critiques, analyzes, or plans a script is not
a video-generation task and must not enter this contract.

**Non-negotiable routing rule:**
`video_generator_production_triggered => video_asset_manager_companion_required`.
When another skill's video-production or actionable-intake trigger has fired,
the host MUST co-invoke this skill in the same turn. Treat this as a mandatory
companion lifecycle, not as a one-of-many skill choice: a more-specific
generator, a tie-breaker, or the absence of a visible manager chip must never
suppress the companion. Do not wait for an explicit `$video-asset-manager`
mention, and do not require this skill to be surfaced as an independent
primary match. This rule applies only to an executable production/intake
workflow; discussion-only turns remain dormant.

Co-invocation is a routing requirement, not a suggestion. The host must treat
the generator's own actionable production trigger as sufficient evidence for
the companion. A bare script, storyboard, shot list, brief, or image is
actionable when the selected generator will execute it or collect operational
generation parameters; it is not actionable when the user is only discussing,
rewriting, translating, critiquing, analyzing, or planning it. Merely loading or
mentioning a generator skill for that non-executable work must not open the
manager.

Active production or actionable intake includes an explicit request to make,
generate, render, animate, continue, or otherwise produce a video; a
confirmation that directly commits to such a request; a production brief that
the selected generator will act on; or the point at which the generator is
about to ask operational generation questions or submit a task. If a selected
generator's own intake contract treats a bare script or image as actionable
production intake, the host classification is sufficient to invoke this
companion before the generator's first operational question. Mere mention of a
video, script, image, hypothetical example, skill name, or possible future work
is not active production.

Use this state sequence, returning structured manager state rather than a
second welcome message:

1. **`generator_selected` (silent open/draft).** Before the generator's first
   operational question or task submission, run `ensure.py open` (or the
   documented local equivalent), reuse the one runtime, and ask the host to
   focus the existing management window on the returned route. When public
   script text or supplied images are already available and the host has
   classified the intake as actionable, immediately run the manager's
   `python <skill>/scripts/vpm_prepare.py prepare` with those inputs (and any
   known public title/type) so the project and public assets are visible while
   the generator continues its intake. This early prepare is idempotent and may
   be repeated at `production_commit`; do not invent missing content. If no
   public inputs or stable project identity are available yet, keep the runtime
   open and continue to the generator's questions without creating guessed
   data. Do not perform this step for discussion-only turns.
2. **`production_commit` (formal preflight).** Immediately before the generator
   submits a task, run `scripts/vpm_prepare.py prepare` (or the documented
   equivalent) with the public title/type/script/assets and known parameters.
   Create or reuse the project, reserve the target clip, persist public inputs,
   open/focus the management window, and pass the returned project/clip/request
   context through unchanged. The returned `model_context` is a sanitized
   delta from the previous project's unacknowledged management edits. Attach it
   to the generator request as explanatory context only; it is never a provider
   prompt or an instruction to execute. Repeating the same preflight is
   idempotent. If the generator needs more user information before submission,
   keep the safe public context and rerun the same preflight when the request is
   committed.
3. **`terminal_result` (receive once).** After every terminal generator result,
   including success, partial output, or explicit failure, invoke
   `scripts/receive.py` or `scripts/vpm_receive.py` exactly once with the public
   result, the original public input when available, and any preflight context.
    Register the result or sanitized outcome; never start a second generation
    attempt from this hook.

### Model context continuity

The manager preserves continuity across “deliver → edit → generate again”
without coupling itself to a generator. `logs/edits.log` is an append-only
explanation of page actions; `project.json` remains the source of truth.

Before a new generation submission, `vpm_prepare.py` reads the active project's
unacknowledged log suffix through `scripts/vpm_context.py read`, coalesces noisy
pointer moves and repeated property changes, and returns a bounded
`model_context` object containing the current manifest snapshot, meaningful
timeline/material changes, revision requests, exports, and draft messages. A
draft `intent.message` is never executed automatically. If the read fails, an
empty context plus a warning is returned and generation continues.

After the host successfully hands that context to the generator, it may
acknowledge exactly the returned cursor with `scripts/vpm_context.py ack`. The
ack is atomic, idempotent, and protected by a log-prefix digest; if the log
changed, reread instead of advancing the marker. Do not acknowledge merely
because a page was opened or a preflight was attempted. Context handoff and
video generation are independent: a failed acknowledgement never retries or
blocks generation.

These additional gates apply outside the companion sequence:

- **Explicit management:** when the user asks to open, inspect, refresh, or
  organize projects, scripts, assets, clips, finals, or logs, run the idempotent
  `ensure.py open` entrypoint and focus the management window without inventing
  a new project unless requested.
- **Discussion-only:** brainstorming, rewriting, translation, critique,
  analysis, hypothetical discussion, and planning without production intent are
  dormant. Do not start the runtime, create a project, or ingest files.
- **Ambiguous intent:** when it is genuinely unclear whether the user wants
  discussion or actual production, ask one short clarification before formal
  project creation. Do not let an isolated "confirm" create a project unless it
  directly follows a committed production request.

All companion actions are best-effort and **fail-open**. A missing launcher,
runtime timeout, port conflict, window-focus failure, asset-intake error, or
receive error must never veto, delay, alter, retry, or otherwise block the
video generator. Start the launcher detached and observe it only for a bounded
interval; a `pending` runtime is a successful launch request, not a generation
failure. Return a concise structured `manager_warning` and preserve safe
context for a later retry, then let the generator continue unchanged. The
manager is generic and must not inspect or reproduce a generator's private
delivery procedure.

For the normal UI flow, run:

```text
python <skill>/assets/webapp/ensure.py open
```

After `ensure.py open` reports a healthy runtime, open or focus the management
window using `window.port=4200` and the returned project route. Window focus is
presentation only: if it fails, leave the runtime running and continue the
generator.

### Host window handoff

The launcher can start the service, while the host UI focuses a window. When a
preflight or terminal receive returns `window.route`, reuse the existing
management window and navigate to that project hash route on port `4200`.
Never register a second application or expose a public URL.
Never wait for a focus operation before invoking the generator. If focus is
unavailable, leave the one runtime alive so the project becomes visible on the
next management open.

`open` is idempotent. It starts or probes the manager on `0.0.0.0:4200`,
health-checks `/api/health`, scans terminal generator handoffs, updates
`index.json`, and returns the port and project route.
The bundled page and runtime keep a small watcher, so completed handoffs appear
without a restart or manual refresh.

### Generation-to-project flow

The normal production path is manager-preflight first, generator unchanged,
then an explicit terminal handoff:

1. On the production-intent gate, run the manager preflight/prepare entrypoint.
   It creates or reuses the project, reserves a `generating` clip, stores the
   user's public script and supplied images, and opens the management window.
   Keep the returned `project_slug`, `clip_id`, and request identifier as
   public handoff context. If this step fails, continue generation and retain
   only safe context for a later retry.
2. Run the selected video-generation skill exactly as its own instructions
   require. The manager does not call a provider, alter prompts, or take over
   generation. If manager preflight fails, continue generation without it.
3. While generation runs, leave the one management runtime available; its page
   can show the reserved clip as generating. Do not restart the runtime for
   ordinary project-data changes.
4. At terminal delivery, pass the public result **and the original public input**
   to `scripts/receive.py` (or `scripts/vpm_receive.py`). Prefer explicit
   `project_slug`/`clip_id`/request context from preflight. Use the
   `--script` value or `input_script`/`public_script` field (or an approved
   `--script-file`/`input_script_file`) for the text the customer supplied,
   never a generator-transformed private prompt.
5. The receive hook stages one atomic, generator-neutral handoff, starts or
   probes the single runtime, and performs one idempotent sync. A completed
   video, public report, audio/media output, and script become visible in the
   same project; a partial or failed result updates the reserved clip with a
   sanitized status. If the hook fails, keep the result/context for a later
   retry and report the manager issue without blocking delivery.

When a generator writes only a local video file and emits no JSON handoff, set
`VIDEO_GENERATOR_OUTPUT_DIR` (or another documented output-directory variable)
to that generator's dedicated output folder before terminal receive or sync.
The bounded scan can import a result into the preflight project, or create a
deterministic project when no project context exists. It never scans arbitrary
`Downloads`, the whole home directory, or the workspace root; when a generator
returns a path in memory, pass that result directly to `receive.py` instead.

If an earlier diagnostic left an unrecorded manager listener on the fixed port,
the launcher may reclaim it only after the listener's health response and
command line both identify this manager. An unrelated service on the port is
never terminated; report the conflict and keep generation fail-open.

The hook accepts local video files and controlled HTTPS/S3 references, and it
does not require the file to be MP4. It recognizes both flat result objects
and common nested envelopes such as `task.content.downloaded_files`. A task's safe `project_slug`, an explicit
map, or `active_project` still takes priority. When none is available, a
terminal result with a usable video receives a deterministic new project;
invalid or incomplete mappings remain pending instead of being guessed.

For a generator that cannot call the hook itself, the host/orchestrator must
 pass its public terminal result to `receive.py`, including the original public
 input when it is available. The manager cannot observe a generator's in-memory
 return value or invent a missing output path; it also never guesses a script
 from private `prompt`, `original_prompt`, or `user_prompt` fields.

For a generator that only creates files, use
`python <skill>/scripts/vpm_receive.py watch <output-dir>` as the host-side
completion watcher. It accepts stable video files or conventional result JSON
files and forwards them through the same generic path; it does not generate or
retry video.

## Workspace and runtime

Resolve the manager root in this order (an explicit `--root` argument, when a
command supports it, wins):

1. `VIDEO_ASSET_MANAGER_ROOT`
2. `VPM_ROOT` (legacy compatibility)
3. `/home/user/workspace/projects` — the shared projects path. It is used when the
   directory already exists, and created when its parent `/home/user/workspace`
   exists. This is where a Capafy instance keeps its projects.
4. `<workspace>/.capafy/video-asset-manager`
5. Windows: `%USERPROFILE%/workspace/.capafy/video-asset-manager`; POSIX:
   `$HOME/workspace/.capafy/video-asset-manager`.

Every entry point (`server.py`, `ensure.py`, `vpm_context.py`, `vpm_record.py`,
`vpm_sync.py`) resolves the root with this same order, and `ensure.py` passes the
result to the child process as `--root`, so the launcher and the service always
agree. An instance created before `/home/user/workspace/projects` was preferred
still lists its projects from the old root until either its project directories
are moved under the new root or `VIDEO_ASSET_MANAGER_ROOT` is set to the old
path.

Use one long-lived manager service for every project. Its default port is
**4200**; do not allocate a port per project. Content changes are read from
disk and appear on the page without a restart. Only a webapp/server upgrade
needs a runtime upgrade operation. `assets/webapp/ensure.py` owns the manager
process lifecycle and fixed port directly; the same launcher works wherever
the skill is installed.

The fixed layout is:

```text
<manager-root>/
  index.json                 # project list, maintained as a projection
  active_project             # current project slug, one line
  inbox/                     # optional generic generator handoff inbox
  trash/                     # soft-deleted/archived project directories
  webapp/                    # installed server, page, ensure script, runtime metadata
  <project-slug>/
    project.json             # schema 2 project manifest and logical asset groups
    state.json               # recoverable high-frequency task state
    assets/
      uploads/               # user-provided files
      generated/             # generated media and public sidecars
      scripts/               # script, storyboard, subtitle source files
      reports/               # customer-visible analysis/retention reports
      groups.json            # optional media-group definitions
    source/
      source.mp4
      segments/
      analysis.json
    clips/<clip_id>/vN.mp4   # append-only clip versions
    output/
      final_vN.mp4
      exports/
    media/
      posters/ proxies/ filmstrips/ waveforms/
    subtitles/
    staging/<generator>/     # transient handoff files; never a public task dump
    tasks/                   # sanitized task summaries and idempotency markers
    logs/
      edits.log processed.marker timeline.jsonl
```

Read `references/project-schema.md` before creating or changing any project
file. Project paths are always relative to that project's root. IDs are stable
and never reused. Clip versions and finals are append-only; create a new
version instead of overwriting a customer-visible result. Deletion is a move
to `trash/` or a status change, not an unlink.

## Canonical project model

New manifests use schema **2** and a singular top-level `asset` object with
four logical collections:

```text
project.asset.scripts  -> script, storyboard, subtitle, public report records
project.asset.media    -> uploaded, generated, and derived media records
project.asset.clips    -> clip/version records
project.asset.finals   -> official final and named export records
```

The physical folders remain conventional and portable; the logical groups in
`project.json` express relationships, labels, lineage, and which files belong
to which clip or final. A schema-1 manifest containing top-level `assets[]`,
`clips[]`, and `assembly` is still readable. On a compatibility write, keep
those legacy fields as a synchronized projection until all consumers support
schema 2. Do not create a second ID for the same file merely to populate an
alias.

`script` is a complete, public chain: the source script, shot list/storyboard,
subtitle source, and any customer-visible analysis report may be registered as
separate records and linked by `related_clips`. Internal prompts, hidden
transformations, provider diagnostics, and private evaluation material are not
script assets and must never be persisted.

## Studio workspace contract

The bundled web application is a lightweight, generator-neutral editing
workspace as well as a project and asset manager. It is not a full non-linear
editor and must not become a second video-generation workflow. The existing
project list, overview, scripts/reports, assets, clips, finals, logs, upload,
download, trim, ZIP, and local export views remain available. The `Studio`
project view and the legacy assembly/concat route are both supported entry
points.

The Studio layout is media-first: a reusable media bin on the left, a player
canvas in the center, an inspector for the selected item on the right, and a
timeline with ruler, playhead, zoom, and snap controls at the bottom. The
persistent editing workspace is separate from the end of media content;
`workspace_duration` controls the ruler/scroll viewport, while preview and
export use the calculated end of playable timeline content. At narrow widths,
the media bin and inspector become drawers and the timeline must not create
page-level horizontal overflow.

The ruler derives its interval from a fixed ladder: 1, 2, 5, and 10 frames,
then 1s, 2s, 5s, 10s, 30s, 1m, 2m, 5m and upward. The ladder is converted with the
project frame rate, deduplicated, and sorted, and the smallest interval whose
neighbouring ticks are at least 60px apart and whose labels cannot collide is the
one that is drawn, so the ruler thins out automatically as the project grows.
Zooming out stops at the whole project (1x, where the lane width equals the
viewport); zooming in stops at one frame per cell, the zoom at which the finest
rung reaches 60px. The playhead, clip drags, drops, and trims always resolve onto
the project frame grid, independently of the snap toggle, because the frame grid
is the timeline's resolution rather than a snapping preference.

The editor supports six bounded tracks (`video-main`, `video-overlay`,
`video-2`, `video-3`, `audio-main`, and `text-main`), PIP transforms, audio
and subtitle tracks, drag/reorder, split, edge trim, bounded speed presets,
undo/redo, frame-level stepping/ruler controls, optional A/V link groups, and
`cut`, `fade`, `dip_black`, and `dip_white` transitions. Subtitles remain manual in this
version, with SRT/VTT import/export and basic style controls. AI editing
controls create a conversation draft only; they never call a model or start
generation.
The detailed timeline shape, operation allowlist, migration rules, and render
contract live in `references/project-schema.md`; read it before changing
timeline data.

Timeline edits are committed as revisioned operations through the manager API.
`assembly.timeline` is authoritative and `assembly.order` is a synchronized
legacy projection. Preview/export work is asynchronous and bounded, so the
4200 HTTP service stays responsive and a failed render cannot corrupt the
timeline. Render outputs are append-only records. Ordinary project or timeline
changes must not restart the service, use HMR, or launch a competing server.

The editor boundary excludes ASR, animated captions, keyframes, masks, filters,
advanced text templates, unlimited tracks, advanced audio mixing, speed curves,
collaboration, publishing, and cloud deployment. Do not silently expand the
manager into those areas.

## Bookkeeping workflow

Use `scripts/vpm_record.py` (or a purpose-built helper in this skill) for every
normal write; do not hand-edit JSON. The recorder must write atomically, bump
`project.rev`, mirror recoverable state, update `index.json`, and append a
sanitized event to `logs/timeline.jsonl`.

Typical moments are:

1. Create/intake: `create --title T [--type clone|generate]`; initialize the
   fixed directories and `active_project`.
2. Source/segments: register the source, segment list, and public analysis.
3. Plan/confirm/start: record public clip plans and execution state only.
4. Delivery: append a new clip version with an honest outcome; refresh
   rebuildable derivatives.
5. Asset registration: register uploads or generated media and, for generated
   files, a public sidecar with only customer-visible prompt/lineage fields.
6. Assembly/official: record the ordered clip IDs, preview, official final,
   and named exports in both the canonical group and compatibility projection.
7. Status/archive: update status and move a deleted project to `trash/`.

After any response that changes a project, run the recorder's `check` command
for that project. If a check fails, re-read the directory and repair through
the recorder before continuing. Never pass raw provider errors, credentials,
or internal routing text into a recorder field.

## Generic generator handoff

When any video-generation skill reports a completed result, perform a
**receive-and-register** operation, never a second generation call. Use the
bundled `scripts/receive.py` completion hook to create the inbox record, then
use the generic `scripts/handoff.py` adapter to register it:

1. Accept only the task status, local/approved output files, public report, and
   user-facing summary needed for the handoff.
   A worker path such as `/home/user/outputs/<name>` or
   `/home/user/agent-outputs/<name>` is treated as a virtual local reference;
   resolve it only against the configured local output/inbox roots, never as a
   remote object key.
2. Stage incoming files under `staging/<generator>/`, then copy them into the
   appropriate `clips/<id>/vN.<video-ext>`, `assets/generated/`,
   `assets/scripts/`, or `assets/reports/` location. A generator may include
   the customer's original/public video input as `input_script` (or
   `public_script`) or an approved `input_script_file`; the bridge sanitizes
   and registers it under `project.asset.scripts` linked to the delivered
   clip. Bare `prompt`, `original_prompt`, and `user_prompt` fields are not
   accepted because they may contain private transformed prompts. If a generator returns a
   direct HTTPS/S3 object URL instead of a local file, cache it during handoff
   and continue with the same local registration; the URL itself is never
   written to a project manifest, log, or ZIP.
   When the delivered video contains an audio stream, the manager also creates
   a deterministic AAC/M4A derivative under `assets/generated/` and registers
   it as `asset.media` with `origin: generated` and an `ai-generated` tag. This
   is a best-effort local derivative: missing ffmpeg, a video-only source, or a
   transcode error never changes a successful video handoff into a failure.
   Repeated scans reuse the same digest-based audio record. The sanitized task
   summary may include `audio.status: unavailable` and a short public message
   when extraction was not possible; this warning is informational only.
3. Register the clip/media/script/final records and write a sanitized task
   summary under `tasks/`. Keep a task ID plus file digest/idempotency marker so
   rescanning the same result is a no-op.
4. Preserve the original generated output unless the customer explicitly asks
   for cleanup; never copy the raw task JSON, private prompt, signed URL,
   provider metadata, or diagnostic trace.
5. For failures or partial results, record only a concise customer-visible
   message and the observable status. A missing report or a remote file that
   cannot be cached is an incomplete handoff, not permission to retry
   generation here.

For a pre-existing inbox record, use the sync entrypoint directly:

```text
python <skill>/scripts/vpm_sync.py
```

It starts/probes the single 4200 runtime and scans terminal JSON handoffs in
`VIDEO_GENERATOR_HANDOFF_DIR` (legacy task directories remain supported). A
safe `project_slug`/`clip_id` supplied by a handoff,
an optional task map (`--map <json>`), or the current `active_project` selects
the destination. When none is available, a completed task with a usable local
video gets a deterministic new project so the video-first flow works; invalid
or incomplete mappings are kept pending. Re-running the command is safe: the
handoff's task ID and output digest prevent a second clip version. Use
`--no-start` when the runtime has already been started by `ensure.py`.
If legacy and current envelopes with the same task ID coexist, the scanner
collapses them before receiving and prefers the one with the richest public
result, so a later status-only envelope cannot erase the original input script
or add another timeline entry.

## Management window and page behavior

The bundled page provides a project list and per-project management views for
 overview/status, scripts and media, clip versions, Studio/timeline editing,
 assembly/export, and logs. It may upload, preview, download, compare, trim to
 a new version, create local concat previews, edit the bounded timeline, export
 presets, and zip a project. AI-oriented buttons create a draft message for the
 conversation; they do not call an AI service.

Start or probe the single runtime with `python <skill>/assets/webapp/ensure.py`.
The launcher also performs the terminal-handoff scan by default, so one
startup normally makes a newly completed generator result visible in the
management window. Do not start a second server or register a second
application for the same manager.
Use `ensure.py sync` (or `ensure.py --sync`) to request an explicit scan and
`--no-sync` when only health probing is wanted. The page uses relative URLs and
hash routing so the host can place it inside the management window.
The page is a view over project files: it must not
be treated as a separate frontend project, and creating a video project must
not register or restart another service.

If `logs/edits.log` exists, ingest its unprocessed tail before planning a
response about that project. Treat `move`/cosmetic entries as presentation
only; apply `assembly.reorder` to future delivery/export order; acknowledge
`asset.upload` as new customer material; treat
`clip.revision_requested` as a revision intake; reflect completed `clip.trim`
and `export.created` operations; and never execute an `intent.message` draft
unless the customer also sends that request in conversation. Website edits
alone never authorize generation, and the customer's message wins when it
conflicts with stale page state.

## Privacy and portability

Every project file, sidecar, log, web response, and ZIP is customer-visible.
Never persist:

- credentials, API keys, environment values, cookies, signed URLs, or tokens;
- private prompts or internal prompt transformations;
- provider names, hidden model/profile/routing/score/rescue details;
- money-related data or raw provider/stack-trace errors;
- absolute paths outside the project root.

The manager never needs a video-generator credential. Its launcher and scanner
subprocesses must preserve ordinary workspace/locale environment settings but
remove every generator credential variable — `vpm_privacy.py` holds the exact
names and the generic `*_API_KEY` pattern — before starting the manager runtime
or a manager-only child process.

Generated sidecars may contain a customer-approved/public prompt, generic
parameter labels, a batch/parent lineage, and creation time. Replace failures
with sanitized customer-facing text. ZIP exports include the public project
manifest, scripts, reports, source, media, clip versions, finals, derivatives,
sidecars, and logs, while excluding secrets, transient private task payloads,
runtime metadata, and files outside the project.

## Deterministic local operations

For operations the page does not cover, use local tools such as ffmpeg only on
authorized files inside the project. Write trims, joins, subtitle burns,
posters, and proxies as new files, then register the result and run `check`.
Do not use this skill to generate video, invoke a provider, publish a site, or
alter any generator's contract.

## Reference

- `references/project-schema.md` -- schema 2, schema-1 compatibility, logical
  asset groups, task handoff records, privacy fields, edit events, and media
  derivative conventions. Read it before writing project data.
