# Video result handoff

These scripts register video-generation results in Video Asset Manager. They
copy approved outputs into a project, record public input scripts and reports,
and update the management workspace. They do not generate videos or retry a
provider request.

## Entry points

| File | Purpose |
| --- | --- |
| `receive.py` | Recommended public completion hook. Accepts a result JSON, video file, or result directory; stages the handoff and runs registration. Starts or probes the manager unless disabled. |
| `handoff.py` | Low-level CLI for registering one task JSON in an **existing** project. Does not start the web server or choose/create a project. |
| `handoff_core.py` | Python import surface shared by the adapters. Not a separate CLI. |
| `handoff_impl.py` | Shared implementation of result validation, media copying, and project registration. Required by `handoff_core.py`; integrations should use `receive.py`. |
| `vpm_receive.py` | File staging, compatible receive/notify commands, and output-directory watching. |
| `vpm_sync.py` | Scans an existing handoff inbox, resolves project mappings, and registers terminal results. |
| `vpm_prepare.py` | Creates or reuses a project and records public inputs before generation. |
| `vpm_record.py`, `vpm_context.py`, `vpm_privacy.py` | Project records, continuation context, and shared privacy helpers. |

Normal integrations call `receive.py` once per terminal result. It delegates
registration internally; do **not** follow it with an additional `handoff.py`
call. Keep the complete scripts directory and bundled webapp together.

## Requirements and paths

- Python 3.10 or newer. The handoff scripts use the Python standard library.
- FFmpeg and ffprobe on `PATH` for media inspection and optional derivatives,
  including audio extraction. Missing derivative tools must not invalidate an
  otherwise successful video handoff.
- A readable generated video or an allowed remote video reference.
- A writable manager data directory outside the published source tree.

Run the examples below from the repository root. `../video-output` and
`../vam-data` are example sibling directories; use your own runtime locations.
Use the same `--root` for preparation, receiving, and the running web server.
The `-B` option prevents Python from writing local bytecode caches into the
source package.

## Receive a generated video

```sh
python -B scripts/receive.py ../video-output/final.mp4 --task-id demo-task-001 --generator example-generator --title "Product demo" --script "A four-second product reveal." --root ../vam-data --bind 127.0.0.1
```

For an existing project, add `--project <existing-slug>` and, when continuing
an existing clip, `--clip <clip-id>`. Reuse identifiers returned by the
preparation step. Keep the same task ID when retrying a delivery; give a new
generation task its own ID.

An explicit project must exist. Without an explicit mapping, the scanner can
use the active project. If there is no destination and a completed result has
a usable video, it can create a deterministic project. Supply a project
explicitly when multiple projects are in progress.

## Receive a result document

Place this example `result.json` next to `final.mp4` in `../video-output`:

```json
{
  "task_id": "demo-task-001",
  "status": "completed",
  "generator": "example-generator",
  "title": "Product demo",
  "video_file": "final.mp4",
  "input_script": "A four-second product reveal.",
  "input_script_role": "script",
  "message": "Video generated."
}
```

```sh
python -B scripts/receive.py --result ../video-output/result.json --root ../vam-data --bind 127.0.0.1
```

Optional fields include `project_slug`, `clip_id`, `report`,
`input_script_file`, and `input_script_name`. A script file may contain a
public script, storyboard, or subtitles. Relative paths inside a result JSON
resolve beside that JSON. For stdin, use `--result -`. Use an absolute path
for a CLI `--video` or `--script-file` override to avoid ambiguity about its
base directory, especially with stdin or a video-only invocation.
File paths inside JSON remain subject to path
validation and do not gain unrestricted access to the host filesystem.

Use `input_script` or `public_script` for customer-visible input. Bare
`prompt`, `original_prompt`, and `user_prompt` fields are intentionally not
treated as public scripts. Do not send credentials, private prompts, raw
provider responses, or diagnostic traces.

Remote video input can use `--video-url` or a result's `video_url` field.
The adapter validates the URL and downloads usable content into local
project storage. It does not authenticate to private buckets using provider
credentials. Remote transport URLs are not intended for public project
records; keep sensitive input envelopes out of the repository.

## Separate registration from runtime startup

| Options on `receive.py` | Behavior |
| --- | --- |
| Default | Stage the result, attempt runtime startup/probing, and scan the inbox. |
| `--no-open` | Stage and scan without starting the runtime. Filesystem registration works even when the server is stopped. |
| `--no-sync` | Stage the result and skip this command's scan; runtime startup is still attempted. A running watcher may receive it. |
| `--no-open --no-sync` | Stage only: no runtime startup and no scan by this command. This still writes an inbox record and may copy media. |

These are not dry-run options. To register without touching the runtime:

```sh
python -B scripts/receive.py --result ../video-output/result.json --root ../vam-data --no-open
```

To stage into a separate inbox, then register it later:

```sh
python -B scripts/receive.py --result ../video-output/result.json --root ../vam-data --task-dir ../video-handoffs --no-open --no-sync
python -B scripts/vpm_sync.py --root ../vam-data --task-dir ../video-handoffs --no-start
```

The default application port is `4200`. Runtime startup and a visible browser
window are separate: a host must consume the returned window handoff to
open/focus its management UI. A startup warning does not mean video generation
failed. See [publishing and network notes](../PUBLISHING.md) before exposing
the port outside a trusted environment.

## Low-level registration and watching

For an **existing** project and an already prepared task document:

```sh
python -B scripts/handoff.py --root ../vam-data --project existing-project --task-file ../video-output/result.json --clip clip-001
```

This adapter validates and registers one document; it is not a replacement
for project preparation or the inbox scanner. Its Python API is
`handoff(root: Path, slug: str, task_file: Path, clip_arg: str | None)` from
`handoff_core`. CLI errors can write diagnostic details to stderr; do not
publish those logs without reviewing them.

For a generator that only writes files, the host can watch its output folder:

```sh
python -B scripts/vpm_receive.py watch ../video-output --root ../vam-data --project existing-project --interval 2 --no-start
```

The watcher waits for stable files and forwards them through the same
registration path. Stop it with Ctrl+C. `--once` performs a single watch pass;
it does not wait indefinitely for an in-progress file to finish.

## Outcomes, retries, and stored data

The completion CLI returns JSON on stdout. Exit `0` indicates the command's
reported success; processing errors return `1`, and invalid CLI usage
normally returns `2`. Inspect the JSON as well:

- `handoff_written` confirms staging, not completed project registration.
- `imported`, `idempotent`, and `pending` describe scanner outcomes. An
  idempotent result reuses an existing registration.
- `project`, `clip_id`, `task_id`, `status`, and `outputs`, when present,
  describe the received result. Check pending/error details rather than
  treating `ok: true` alone as proof that a playable clip exists.
- `manager_warning` and `runtime` concern the management service. They do
  not authorize restarting generation.

Unchanged task IDs and content digests prevent duplicate registration. A
changed public result may update its records; a different video can create
a new version. Failed, partial, and cancelled terminal results can also be
recorded, but do not imply that a video file exists. Fix destination mapping
or file availability and retry receiving the same result instead of
regenerating it.

Video versions are stored under the project's `clips/` directory. Public
scripts and reports go under `assets/scripts/` and `assets/reports/`.
When audio extraction succeeds, a generated audio asset is added under
`assets/generated/` with generated provenance. Sanitized task summaries go
under `tasks/`. Original generator outputs are preserved.

For the complete data contract, see
[Generic handoff protocol](../references/project-schema.md#generic-handoff-protocol).
Use `python -B scripts/receive.py --help` for the current CLI options.
