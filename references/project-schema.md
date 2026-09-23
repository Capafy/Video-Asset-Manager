# Project schema reference (video-asset-manager)

This is the authoritative data contract for the manager workspace. Schema **2**
adds a canonical logical `asset` grouping while retaining schema-1 fields for
the bundled page and older recorder clients. Normal writes go through
`scripts/vpm_record.py` or an equivalent manager helper; these definitions are
also the contract for a last-resort hand edit. Every path stored here is
relative to the project root, every write is atomic, and `project.rev` advances
on each manifest write. Privacy rules in `SKILL.md` apply to every field.

## Workspace layout

```text
<manager-root>/
  index.json
  active_project
  inbox/                     # optional generic generator handoff inbox
  trash/
  webapp/
  <project-slug>/
    project.json
    state.json
    assets/
      uploads/
      generated/
      scripts/
      reports/
      groups.json
    source/
      source.mp4
      segments/
      analysis.json
    clips/<clip_id>/
      v1.mp4 v2.mp4
      meta.json
    output/
      final_v1.mp4
      exports/
    media/
      posters/ proxies/ filmstrips/ waveforms/
    subtitles/
    staging/<generator>/
    tasks/
    logs/
      edits.log
      processed.marker
      timeline.jsonl
```

The directory is the portable project boundary: copying it copies the project;
derived media may be rebuilt. `project.json` describes what the project is,
`state.json` describes current progress, and `index.json` is the list-page
projection. Never use an absolute path in a project field.

## Invariants

1. New manifests use `schema: 2`; readers must accept schema 1 and manifests
   that predate the `asset` field.
2. IDs are stable, content-derived or explicitly assigned, and never reused.
   Recommended prefixes are `seg_`, `scr_`, `as_`, `clip_`, `final_`, and
   `task_`. Do not use Python/JavaScript runtime hashes or random IDs.
3. Clip versions and finals are append-only (`vN`); do not overwrite a
   customer-visible file. Mark an older record `superseded` when appropriate.
4. Paths are normalized project-relative paths using `/`; reject empty paths,
   `..`, drive letters, UNC paths, and symlink escapes.
5. `project.json` is written through a temp file followed by an atomic rename;
   increment `rev` exactly once per successful manifest write.
6. `index.json`, `active_project`, `state.json`, and timeline events are kept
   synchronized by the recorder. A crash may lose recoverable state, but not a
   committed manifest write.
7. Deletion is soft: move a project under `trash/` or mark a record
   `superseded`/`trashed`; do not unlink the only customer-visible result.

## Schema-1 compatibility

Older projects commonly contain top-level `assets[]`, `clips[]`, and
`assembly`. A reader should normalize them in memory as follows:

- `assets[]` becomes `asset.media[]`, except records whose public role/kind is
  `script`, `storyboard`, `subtitle`, or `analysis_report` (or whose path is
  under `assets/scripts/`, `assets/reports/`, or `subtitles/`) become
  `asset.scripts[]`.
- `clips[]` becomes `asset.clips[]` without changing clip IDs or version files.
- `assembly.official` and `assembly.exports[]` become `asset.finals[]`; use a
  stable ID such as `final_official` or a digest of the project-relative file
  path, never a random ID.
- Missing `asset` collections are treated as empty; unknown top-level fields
  are preserved.

When a normalized project is saved, write `schema: 2` and mirror the legacy
projection (`assets[]`, `clips[]`, and `assembly`) with the same IDs and paths.
Until all consumers migrate, a writer must accept either representation and
must not create duplicate records solely to satisfy an alias. A schema-1 file
may remain schema 1 when a read-only operation does not need to write it.

## `index.json`

The list page reads the manager-root index. It is a projection and can be
recomputed from project manifests; it is not a replacement for `project.json`.
The current shape remains compatible with schema-1 clients:

```json
{
  "schema": 1,
  "updated": "2026-08-31T12:00:00Z",
  "projects": [
    {
      "slug": "2026-08-31-product-demo",
      "title": "Product demo",
      "type": "generate",
      "status": "reviewing",
      "cover": "2026-08-31-product-demo/media/posters/clip_01.jpg",
      "clips_done": 1,
      "clips_total": 1,
      "finals_done": 1,
      "updated": "2026-08-31T11:59:00Z",
      "size_bytes": 183500000
    }
  ]
}
```

`finals_done` and `size_bytes` are optional. All paths in the projection are
relative to the manager root (the project slug is the first component).

## `project.json` (schema 2)

The following is a compact valid shape. Collection entries are specified below;
legacy aliases are shown explicitly because older webapp code still consumes
them.

```json
{
  "schema": 2,
  "rev": 0,
  "slug": "2026-08-31-product-demo",
  "title": "Product demo",
  "type": "generate",
  "status": "draft",
  "created": "2026-08-31T11:00:00Z",
  "updated": "2026-08-31T11:00:00Z",
  "presets": {"ratio": "9:16", "clarity": "Standard"},
  "source": {
    "file": null,
    "origin_url": null,
    "duration": null,
    "segments": [],
    "analysis": null
  },
  "asset": {
    "scripts": [],
    "media": [],
    "clips": [],
    "finals": []
  },
  "assets": [],
  "clips": [],
  "assembly": {
    "order": [],
    "transition": "cut",
    "schema": 2,
    "canvas": {"ratio": "9:16", "width": 1080, "height": 1920, "background": "#000000"},
    "timeline": {
      "zoom": 1,
      "fps": 30,
      "workspace_duration": 7,
      "snap": true,
      "tracks": [
        {"id": "video-main", "kind": "video", "muted": false, "hidden": false, "clips": []},
        {"id": "video-overlay", "kind": "video", "muted": false, "hidden": false, "clips": []},
        {"id": "audio-main", "kind": "audio", "muted": false, "hidden": false, "clips": []},
        {"id": "text-main", "kind": "subtitle", "muted": false, "hidden": false, "cues": []}
      ]
    },
    "audio": {"bgm": null, "bgm_gain": 0, "mute_original": false},
    "subtitles": {"file": null, "burn": false},
    "preview": null,
    "official": null,
    "exports": []
  }
}
```

### `asset.scripts[]`

Scripts are public chain resources, not hidden generation instructions. Each
record may contain:

```json
{
  "id": "scr_main",
  "role": "script",
  "file": "assets/scripts/script.md",
  "name": "Main script",
  "format": "markdown",
  "related_clips": ["clip_01"],
  "status": "active",
  "created": "2026-08-31T11:02:00Z"
}
```

`role` is one of `script`, `storyboard`, `subtitle`, or `analysis_report`.
Use `assets/scripts/` for script/storyboard/subtitle source files and
`assets/reports/` for customer-visible reports. `related_clips` may be empty.
Keep only the approved/public text; private prompts and internal analysis do
not belong here.

### `asset.media[]` (legacy `assets[]`)

```json
{
  "id": "as_logo",
  "kind": "image",
  "file": "assets/uploads/logo.png",
  "name": "Logo",
  "origin": "upload",
  "group": null,
  "tags": ["brand"],
  "hash": "sha256:...",
  "poster": null,
  "used_by": ["clip_01"],
  "status": "active"
}
```

`kind` is `image`, `video`, `audio`, `text`, or `font`; `origin` is `upload`,
`generated`, or `derived`; `status` is `active`, `superseded`, or `trashed`.
Generated records may add:

```json
{
  "gen": {
    "prompt_summary": "Customer-visible summary",
    "batch": "b_20260831_01",
    "parent": "as_reference",
    "sidecar": "assets/generated/logo.png.json"
  }
}
```

The sidecar is described below. Do not put a private prompt in `gen`.

### `asset.clips[]` (legacy `clips[]`)

```json
{
  "id": "clip_01",
  "segment": null,
  "title": "Opening",
  "duration": 15,
  "ratio": "9:16",
  "clarity": "Standard",
  "mappings": [],
  "preserve": "Pacing and edit points",
  "status": "planned",
  "outcome": null,
  "current": null,
  "versions": [],
  "poster": null,
  "proxy": null,
  "filmstrip": null,
  "revision_note": null,
  "handoff_status": null,
  "handoff_message": null,
  "handoff_task_id": null,
  "handoff_updated": null
}
```

Clip status is `planned`, `confirmed`, `generating`, `delivered`,
`revision_requested`, or `superseded`. A delivered clip has at least one
version and an honest outcome (`complete`, `partial`, `zero_replacement`,
`met`, `partially_met`, or `missed`). Version entries are append-only:

When a preflight-reserved clip receives a failed or partial generator result,
the legal clip `status` is retained (normally `generating`) and the optional
`handoff_status`, `handoff_message`, `handoff_task_id`, and
`handoff_updated` fields describe that transport outcome.  The manager does
not add a phantom clip for an unplanned failure, and a failed late result never
downgrades a `delivered` or `superseded` clip.  `revision_note` mirrors the
sanitized message for older page versions.

```json
{
  "v": 1,
  "file": "clips/clip_01/v1.mp4",
  "created": "2026-08-31T11:30:00Z",
  "note": "First delivery",
  "superseded": false
}
```

The video extension is not a type requirement. A generator handoff preserves a
recognized source container such as `.mp4`, `.webm`, `.mov`, or `.mkv` after
caching a remote object-store URL. Project manifests still contain only the
project-relative cached file; signed/remote URLs are never persisted.

### `asset.finals[]` and `assembly`

`asset.finals[]` is the canonical logical list of finished/preview outputs:

```json
{
  "id": "final_official",
  "kind": "official",
  "file": "output/final_v1.mp4",
  "name": "Official final",
  "preset": null,
  "from": ["clip_01"],
  "created": "2026-08-31T11:45:00Z",
  "status": "active"
}
```

`kind` is `preview`, `official`, or `export`; `status` is `active` or
`superseded`. The compatibility `assembly` object keeps `order`, `preview`,
`official`, and `exports[]` fields. It also contains the schema-2 `canvas` and
`timeline` fields described below. Every change to one representation must
update the other in the same manifest write.

## `assembly.timeline` (schema 2)

`assembly.timeline` is the authoritative, revisioned edit model. It references
logical media and clip versions; it never stores absolute paths or duplicates
the source files. `assembly.order` remains a synchronized projection of the
primary `video-main` track for schema-1 clients.

The V1 track contract is intentionally bounded:

| Track ID | Kind | Capacity | Purpose |
| --- | --- | --- | --- |
| `video-main` | `video` | one ordered lane | primary sequence and export order |
| `video-overlay` | `video` | one bounded lane | primary picture-in-picture layer |
| `video-2` | `video` | one bounded lane | secondary picture-in-picture layer |
| `video-3` | `video` | one bounded lane | tertiary picture-in-picture layer |
| `audio-main` | `audio` | one bounded lane | original or supplied audio |
| `text-main` | `subtitle` | one cue lane | manual subtitles and text |

The shape below is normative. Times are seconds from the timeline origin;
`start` is the timeline position, while `in` and `out` are source-relative
trim points. `duration` is the effective duration after trimming and speed.
`workspace_duration` is the persistent editing canvas length and is deliberately independent from the content end used for preview/export. It defaults to 7 seconds for short-form projects and grows when clips are placed beyond it; a short clip must not collapse the ruler or horizontal editing viewport.

```json
{
  "schema": 2,
  "zoom": 1,
  "fps": 30,
  "workspace_duration": 7,
  "snap": true,
  "tracks": [
    {
      "id": "video-main",
      "kind": "video",
      "muted": false,
      "hidden": false,
      "clips": [
        {
          "id": "tl_main_01",
          "clip_id": "clip_01",
          "version": 1,
          "start": 0,
          "in": 0,
          "out": 4,
          "duration": 4,
          "speed": 1,
          "transform": {"x": 0, "y": 0, "scale": 1, "rotate": 0},
          "link_group": null,
          "transition_in": {"kind": "cut", "duration": 0},
          "transition_out": {"kind": "fade", "duration": 0.3}
        }
      ]
    },
    {"id": "video-overlay", "kind": "video", "muted": false, "hidden": false, "clips": []},
    {"id": "video-2", "kind": "video", "muted": false, "hidden": false, "clips": []},
    {"id": "video-3", "kind": "video", "muted": false, "hidden": false, "clips": []},
    {"id": "audio-main", "kind": "audio", "muted": false, "hidden": false, "clips": []},
    {"id": "text-main", "kind": "subtitle", "muted": false, "hidden": false, "cues": []}
  ]
}
```

Video timeline items must contain `clip_id` and an existing append-only
`version`; video items may use a bounded `transform` with `x`, `y`, `scale`, `rotate`, `opacity`, and `border` fields (the latter two are primarily for picture-in-picture). Audio items may contain
`media_id`, `start`, `in`, `out`, `gain`, `fade_in`, `fade_out`, and
`detached_from_video`. Subtitle cues contain a stable `id`, `start`, `end`,
`text`, and optional public style fields (`font`, `fontSize`, `color`,
`outlineColor`, `outlineWidth`, `background`, `backgroundOpacity`, `position`,
`align`). Legacy `size` and `stroke` aliases remain readable. Reject negative
times, `out <= in`, invalid speeds,
unknown track IDs, missing references, and overlapping items that exceed the
track contract.

Allowed V1 operations for a timeline commit are:
`add`, `remove`, `move`, `split`, `trim`, `duplicate`, `set_speed`,
`set_transform`, `set_link_group`, `set_caption`, `set_audio`, `set_transition`, `set_canvas`, `set_track`,
`undo`, `redo`, `replace_timeline`, `close_gap`, `close_gap_before`,
`ripple_delete`, `ripple_remove`, and `shift_track`. The latter aliases are
bounded editor operations: `close_gap_before` is the preceding-gap variant,
`ripple_remove` aliases `ripple_delete`, and `replace_timeline` replaces the
validated timeline atomically. The server validates an operation allowlist and fields,
applies the whole batch against `base_rev`, writes one atomic manifest update,
and synchronizes `assembly.order`. A rejected batch changes nothing.

### Timeline migration and revision API

For an older manifest containing only `assembly.order`, normalize in memory to
one `video-main` track with `cut` transitions. Do not bump the revision for a
read-only migration; persist schema 2 at the first explicit timeline save.
Never create duplicate logical records merely to populate a compatibility
alias.

The manager exposes these public endpoints:

```text
GET  /api/project/{slug}/timeline
POST /api/project/{slug}/timeline/commit
     {"base_rev": 12, "operations": [{"op": "move", "item_id": "tl_main_01", "start": 1.5}]}
POST /api/project/{slug}/render
     {"timeline_rev": 13, "mode": "preview"|"export", "preset": {...}}
GET  /api/project/{slug}/render/{job_id}
```

`timeline/commit` uses optimistic concurrency. A stale `base_rev` returns
HTTP `409` with the current public revision and leaves the project unchanged.
All writes are atomic, project-relative, privacy-filtered, and logged without
private prompts or provider data. Keep `/assemble`, `/trim`, and `/export` as
compatibility adapters for older page clients.

## Canvas, captions, audio, and transitions

The supported canvas ratios are `16:9`, `9:16`, `1:1`, and `4:5`; store the selected
ratio, concrete dimensions, and a six-digit `background` hex color in `assembly.canvas`.
Timeline `fps` is bounded to 24, 25, 30, 50, or 60 (default 30). V1 transform values are
bounded numeric `x`, `y`, `scale`, `rotate`, `opacity`, and `border` fields.
A transform `scale` above `1` zooms the clip: the canvas keeps the centre of the
scaled frame plus the `x`/`y` offset and clips whatever overflows, exactly as the
live canvas shows it. The renderer must pad **and** crop for that geometry — a
`pad` alone fails with `Padded dimensions cannot be smaller than input dimensions`
and the export reports `unable to encode timeline part N` while the preview still
looks correct.
Manual subtitle cues may be imported/exported as SRT or VTT and must remain
public text.

`cut`, `fade`, `dip_black`, and `dip_white` are valid transition kinds. `fade` is an
adjacent-clip cross-dissolve, with a default duration of `0.30` seconds, clamped to the
lesser of `2.0` seconds and 50% of either neighboring usable duration. It does
not add an implicit fade at the beginning or end. `dip_black` fades the
outgoing clip to black and fades the incoming clip from black using the same
bounded duration rules; it is rendered with FFmpeg's `fadeblack` path. `dip_white`
uses the equivalent FFmpeg `fadewhite` path. A
two-clip sequence of two four-second sources with a 0.3-second fade is
approximately 7.7 seconds.

A transition may carry an optional `easing` field: `linear` (default),
`ease_in`, `ease_out`, or `ease_in_out`. Easing applies only to the `fade`
cross-dissolve; it is baked into an FFmpeg `xfade` custom expression whose
weights stay affine (`A*(1-W)+B*W`, `W` in `[0,1]`), and the editor monitor
mirrors the identical curve, so preview and export stay pixel-consistent.
`dip_black` always renders through the built-in `fadeblack` transition (its
smoothstep curve has no easing variant); an `easing` value on a `dip_black`
or `cut` transition is stored but ignored by the renderer. Unknown easing
values are rejected; absent values normalize to `linear` and are not written
back, keeping legacy projects byte-identical.

The primary video sequence is the bottom-most video lane that holds a clip:
`video-main`, otherwise `video-overlay`, then `video-2`, then `video-3`. The
editor monitor, the transition lookups, and the renderer resolve that same lane,
so a project whose clips sit on an overlay row previews exactly what it exports.
Every lane above the primary one is a picture-in-picture layer.

Track state is part of the contract. `hidden` removes a track's picture: a
hidden subtitle track is not burned in, a hidden video lane is not composited,
and a hidden `audio-main` lane contributes nothing. Hiding a video lane never
removes its soundtrack -- only `muted` does that, so a hidden primary track still
plays (the canvas shows the background colour) while a muted one contributes
nothing. Every video lane keeps its own audio: a picture-in-picture overlay is
mixed into the export from its own source, and its `muted` flag removes exactly
that contribution. Muting the primary lane still leaves the standalone
`audio-main` lane playing, and muting every source delivers a file with no audio
stream. The editor monitor follows both flags, so what the timeline shows is what
the export produces.

A finished export is a library entry with `origin: "export"` (served from
`asset.finals[]`), so it appears under ALL in the assets list alongside
generations and uploads, and the list is refreshed when an export completes.

Project files are served through the API because a hosted preview proxies `/api/...`
only. Two routes exist for the same bytes: `GET /api/project/<slug>/download?file=<rel>`
answers with an attachment name (and `inline=1` switches the disposition), while
`GET /api/project/<slug>/file/<rel>` is a query-free, always-inline address with
Range support that can be pasted anywhere as the source location of the file. Neither
route redirects; HEAD is answered on both.

Gaps never disable a transition. A boundary into or out of an intentional gap --
including a leading offset before the first clip -- is a hard cut, while a
boundary between two touching clips keeps its transition. The rendered length is
the sum of the segments minus the transitions that overlap them, so two
four-second sources with a 0.3-second fade are 7.7 seconds with or without gaps.

Renderers normalize video dimensions, frame rate, pixel format, and audio
sample rate. Use FFmpeg `xfade` for video and `acrossfade` for audio; supply
silence for a source without audio so A/V duration stays aligned. Preview and
export are asynchronous bounded jobs and append new `asset.finals[]` records;
they never overwrite the timeline or an existing customer-visible output.

## `state.json`

State is high-frequency and recoverable. It must not contain secrets or raw
provider errors:

```json
{
  "schema": 1,
  "rev": 4,
  "phase": "generating",
  "active_clip": "clip_01",
  "queue": [
    {"clip": "clip_01", "status": "generating", "started": "2026-08-31T11:20:00Z"}
  ],
  "last_error": {"clip": null, "message": null},
  "updated": "2026-08-31T11:20:10Z"
}
```

Allowed phases are `draft`, `planning`, `generating`, `reviewing`, `done`, and
`archived`. `last_error.message` is a sanitized customer-facing sentence.

## Generator handoff records

The manager receives completed outputs from any video-generation skill; it does
not replay a task. Store only a sanitized summary in
`tasks/<task_id>.json`:

```json
{
  "schema": 1,
  "task_id": "task_public_01",
  "source": "video-generator",
  "status": "completed",
  "received": "2026-08-31T11:40:00Z",
  "clip_id": "clip_01",
  "outputs": [
    {"file": "clips/clip_01/v1.mp4", "kind": "clip", "sha256": "sha256:..."}
  ],
  "audio": {"status": "registered", "file": "assets/generated/audio-clip_01-....m4a"},
  "report": "assets/reports/clip_01-retention.md",
  "message": "Video and public report received.",
  "idempotency": "sha256:..."
}
```

`status` is `completed`, `partial`, or `failed`. A failed/partial record may
contain only an observable status and sanitized message. Never copy raw task
JSON, private prompts, provider/model metadata, signed URLs, credentials, or
diagnostic traces. The task ID plus output digest makes a repeated scan
idempotent; it must not append a second version for the same handoff.

The optional `audio` object reports the best-effort audio derivative. Its
`status` is `registered` with a project-relative `file`, or `unavailable` with
a short customer-facing `message`. Audio extraction failures do not change a
successful video delivery to `partial` or `failed`.

## Generated-media sidecars

For `asset.media[]` records with `origin: generated`, place a sidecar next to
the file, for example `assets/generated/mascot.png.json`:

```json
{
  "prompt": "Customer-approved public prompt",
  "model_label": "image",
  "params": {"size": "1024x1024"},
  "batch": "b_20260831_01",
  "parent": "as_reference",
  "created": "2026-08-31T11:10:00Z"
}
```

`prompt` must be public/customer-approved. `model_label` is a generic label,
not a provider or internal route. Omit private prompt transformations, hidden
parameters, scores, rescue data, cost, and raw errors.

### Extracted audio from generated videos

When a delivered video has an audio stream, the receive hook may create a
playable AAC/M4A derivative at a deterministic path such as
`assets/generated/audio-clip_01-<video-digest>.m4a`. The corresponding
`asset.media[]` record uses `kind: "audio"`, `origin: "generated"`, and an
`ai-generated` tag so the management page's generated-media filter includes it:

```json
{
  "id": "as_audio_...",
  "kind": "audio",
  "file": "assets/generated/audio-clip_01-...m4a",
  "name": "AI generated audio · clip_01",
  "origin": "generated",
  "group": "ai-generated",
  "tags": ["ai-generated", "extracted-audio"],
  "used_by": ["clip_01"],
  "status": "active",
  "hash": "sha256:...",
  "gen": {
    "prompt_summary": "Audio track extracted from an AI-generated video.",
    "model_label": "audio",
    "params": {"operation": "extract-audio", "source": "video"},
    "parent": "clip_01",
    "source_video": "clips/clip_01/v1.webm",
    "source_sha256": "sha256:...",
    "sidecar": "assets/generated/audio-clip_01-....m4a.json"
  }
}
```

Audio extraction is best effort and fail-open. A missing ffmpeg binary, a
video-only source, or a transcode error leaves the delivered video and its
manifest intact; no fabricated audio path or media record is written. The
source video digest is part of the stable identity, so rescanning the same
handoff does not append another audio record.

## `logs/edits.log` vocabulary

The page writes one JSON object per line. The manager ingests only entries after
`processed.marker`:

| `op` | Required fields | Meaning |
| --- | --- | --- |
| `move` / `cosmetic` | `id`, presentation fields | Ignore for generation decisions. |
| `assembly.reorder` | `order[]`, `ts` | Delivery/export preference. |
| `asset.upload` | `id`, `name`, `path`, `ts` | New customer material. |
| `clip.revision_requested` | `clip`, `note`, `ts` | Revision intake. |
| `clip.trim` | `clip`, `in`, `out`, `new_version`, `file` | Completed local trim. |
| `export.created` | `file`, `preset`, `from` | Completed local export. |
| `intent.message` | `text`, `ts` | Draft only; never execute by itself. |

Website edits do not authorize generation. If page state conflicts with the
customer's conversation message, the conversation message wins.

## Model context cursor contract

`logs/processed.marker` is a small JSON document maintained by the host-side
context bridge, not by the web page. Its shape is:

```json
{"schema": 1, "next_line": 138, "prefix_sha256": "sha256-of-log-prefix"}
```

`python scripts/vpm_context.py read --project <slug>` reads only entries from
`next_line` onward and returns a bounded, privacy-filtered `model_context`:

- `project` is a compact snapshot of the current manifest/timeline;
- `summary` separates new material, revision requests, trims, exports,
  timeline changes, meaningful property changes, and draft messages;
- `cursor` identifies the exact log prefix that was read;
- `notes_for_generator` explains that the manifest is authoritative and draft
  messages are not commands.

The reader tolerates an incomplete final JSONL line and never executes a draft.
The host may call `python scripts/vpm_context.py ack --project <slug> --cursor
'<cursor-json>'` only after the context was successfully delivered to the
generator. The bridge verifies the current log prefix before atomically writing
the marker. A changed prefix is a conflict and requires another read. Failure
to read or acknowledge is fail-open and must not veto video generation.

## Privacy and ZIP rules

All manifests, sidecars, logs, task summaries, API responses, and ZIPs are
customer-visible. Exclude credentials, API keys, environment values, cookies,
signed URLs, private prompts, internal transformations, provider names,
hidden model/routing/score/rescue details, money data, raw stack traces, and
absolute paths outside the project. ZIP export includes public scripts,
reports, media, source, clips, finals, derivatives, sidecars, and logs; exclude
runtime metadata, transient private task payloads, and files outside the
project root.

## Media derivatives

Derivatives are rebuildable and should never replace an original. When ffmpeg
is available, these are representative commands (write to project-relative
destinations):

```text
ffmpeg -y -ss <mid> -i IN.mp4 -frames:v 1 -vf scale=640:-2 media/posters/<id>.jpg
ffmpeg -y -i IN.mp4 -vf "fps=10/<dur>,scale=160:-2,tile=10x1" media/filmstrips/<id>.jpg
ffmpeg -y -i IN.mp4 -vf scale=480:-2 -c:v libx264 -preset veryfast -crf 30 -c:a aac -b:a 64k media/proxies/<id>.mp4
```

If ffmpeg is unavailable, retain the original and record a sanitized warning;
do not fabricate a derivative path.

## Generic handoff protocol

Every video-generation skill can deliver to the manager by writing one JSON
document into `VIDEO_GENERATOR_HANDOFF_DIR` (or a configured generator task
directory).
The file is an inbox message, not a project file, and may be removed by the
producer after the manager records its idempotency marker. A minimal payload is:

```json
{
  "task_id": "stable-generator-task-id",
  "status": "completed",
  "generator": "my-video-skill",
  "project_slug": "optional-existing-project",
  "clip_id": "optional-clip-id",
  "video_file": "C:/approved/output/video.webm",
  "report": "C:/approved/output/report.md",
  "input_script": "Original public video input script or brief",
  "input_script_file": "C:/approved/output/script.md",
  "input_script_role": "script",
  "input_script_name": "Original input",
  "message": "Video generated."
}
```

`input_script` (or the explicit `public_script` alias) is the only inline
script field accepted by the generic bridge. `input_script_file` points to a
public text/Markdown/storyboard/subtitle file under an approved output root.
The bridge filters credentials, private prompt/routing fields, URLs, and
absolute paths, then copies the cleaned text into `assets/scripts/` and
registers one `asset.scripts[]` record linked to the delivered clip. A bare
`prompt`, `original_prompt`, or `user_prompt` field is deliberately ignored;
generators must mark the customer-visible input explicitly. Repeated scans
use the content digest for a stable script ID and do not create duplicates.

The preferred completion bridge is `scripts/receive.py` (the compatible
`scripts/vpm_receive.py` command also remains available). It accepts a result
JSON, a local video file, or a result directory, stages the public fields, and
then starts/probes the manager and runs one sync. Flat results and common
runner envelopes such as `task.content.downloaded_files` are both accepted.
A generator does not need to
know the manager's project folders or call a provider-specific adapter. For a
generator that only emits files, `vpm_receive.py watch <output-dir>` can be
used as a bounded host-side watcher; it waits for a stable file and forwards
it through the same protocol.

`status` may be `completed`, `success`, `failed`, `partial`, or another
terminal status recognized by the scanner. A video may be supplied as a local
file (`video_file`, `video_path`, `local_video`) or as a controlled `https`/
`s3` reference (`video_url`, `download_url`, `object_url`); the manager caches
remote content and stores only a project-relative local file. `generator` is a
public label used for staging and provenance. The manager ignores unknown
private fields, never calls a generator, and never retries generation. The
manager cannot see an in-memory return value from another skill; the host must
invoke the completion bridge or write the inbox document.

If no project is specified, the manager uses `active_project`; when that is
also absent, a completed handoff with a usable video creates a deterministic
project. Re-scanning the same `task_id` and file digest is a no-op.
