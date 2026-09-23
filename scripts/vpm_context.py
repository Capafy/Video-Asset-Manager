#!/usr/bin/env python3
"""Build a small, generator-neutral context delta from manager edit logs.

The edit log is an explanation of user actions, not project state.  This
module reads only the unacknowledged suffix, removes presentation noise,
coalesces repetitive operations, and emits a bounded JSON object that a host
can attach to the next video-generation request.  It never calls a provider
and never executes intent drafts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

try:
    from vpm_privacy import contains_private_text, redact_recursive, sanitize_public_text
except Exception:  # pragma: no cover - copied bundle compatibility
    def contains_private_text(value: object, *, allow_public_url: bool = False) -> bool:
        return bool(re.search(r"(?:api[_-]?key|secret|token|password|provider|signed[_-]?url|[A-Za-z]:[\\/]|/(?:home|users|tmp|workspace)/)", str(value or ""), re.I))

    def sanitize_public_text(value: object, *, limit: int = 4000, fallback: str | None = None, allow_public_url: bool = False) -> str | None:
        text = str(value or "").replace("\x00", "").strip()
        return fallback if not text or len(text) > limit or contains_private_text(text) else text

    def redact_recursive(value: object, *, depth: int = 0) -> object:
        if depth > 8:
            return None
        if isinstance(value, dict):
            return {str(k): redact_recursive(v, depth=depth + 1) for k, v in value.items() if not contains_private_text(k)}
        if isinstance(value, list):
            return [redact_recursive(v, depth=depth + 1) for v in value]
        if isinstance(value, str):
            return sanitize_public_text(value, limit=4000)
        return value if isinstance(value, (int, float, bool)) or value is None else None


DEFAULT_ROOT = Path.home() / "workspace" / ".capafy" / "video-asset-manager"
SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
NOISE_OPS = {"move", "cosmetic", "render.completed", "assembly.preview_built"}
PROPERTY_OPS = {"set_transform", "set_speed", "set_transition", "set_caption", "set_audio", "set_canvas", "close_gap"}
MAX_EVENTS = 5000
# A host that never acknowledges would receive the whole log on every read, which
# grows without bound.  Deliver at most this many of the most recent unacknowledged
# lines instead; the log file itself is untouched and remains the source of truth.
MAX_CONTEXT_WINDOW = 400
MAX_TEXT = 2000


def resolve_root(explicit: str | None) -> Path:
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    value = os.environ.get("VIDEO_ASSET_MANAGER_ROOT") or os.environ.get("VPM_ROOT")
    if value:
        return Path(os.path.abspath(os.path.expanduser(value)))
    # Capafy instances keep /home/user/workspace across resets, so prefer the team's
    # agreed projects path when it exists, or create it when its persistent parent
    # does.  Kept in lock-step with server.py so every entry point agrees.
    candidate = "/home/user/workspace/projects"
    parent = os.path.dirname(candidate)
    if os.path.isdir(candidate):
        return Path(candidate)
    if os.path.isdir(parent):
        try:
            os.makedirs(candidate, exist_ok=True)
            return Path(candidate)
        except OSError:
            pass
    workspace = os.environ.get("CAPAFY_WORKSPACE")
    if workspace:
        return Path(os.path.abspath(os.path.join(os.path.expanduser(workspace), ".capafy", "video-asset-manager")))
    return DEFAULT_ROOT


def project_dir(root: Path, slug: str) -> Path:
    if not SAFE_SLUG.fullmatch(slug):
        raise ValueError("project slug is invalid")
    path = (root / slug).resolve()
    if path.parent != root.resolve():
        raise ValueError("project path escapes manager root")
    return path


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def marker_path(project: Path) -> Path:
    return project / "logs" / "processed.marker"


def read_marker(project: Path) -> dict[str, Any]:
    try:
        value = json.loads(marker_path(project).read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
        if isinstance(value, int):
            return {"next_line": max(0, value)}
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    # Accept the old one-line integer marker for forwards compatibility.
    try:
        text = marker_path(project).read_text(encoding="utf-8").strip()
        if text.isdigit():
            return {"next_line": int(text)}
    except (OSError, UnicodeError):
        pass
    return {"next_line": 0}


def log_records(project: Path, start: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = project / "logs" / "edits.log"
    if not path.is_file():
        return [], {"start_line": start, "end_line": start, "next_line": start, "prefix_sha256": hashlib.sha256(b"").hexdigest(), "log_sha256": hashlib.sha256(b"").hexdigest(), "warnings": []}
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    start = max(0, min(int(start), len(lines)))
    prefix_digest = hashlib.sha256(b"".join(lines[:start])).hexdigest()
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    next_line = start
    window_start = start
    skipped_lines = 0
    pending = lines[start:]
    if len(pending) > MAX_CONTEXT_WINDOW:
        skipped_lines = len(pending) - MAX_CONTEXT_WINDOW
        window_start = start + skipped_lines
        pending = pending[skipped_lines:]
        warnings.append(
            f"delivered the most recent {MAX_CONTEXT_WINDOW} of {len(pending) + skipped_lines} "
            f"unacknowledged edit-log lines; earlier events are summarised by the manifest"
        )
    for index, line in enumerate(pending, start=window_start):
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if index == len(lines) - 1:
                warnings.append("ignored incomplete trailing edit-log line")
                break
            warnings.append(f"ignored malformed edit-log line {index + 1}")
            next_line = index + 1
            continue
        if isinstance(value, dict):
            records.append(value)
        next_line = index + 1
        if len(records) >= MAX_EVENTS:
            warnings.append("edit-log context was bounded")
            break
    end = next_line
    # Ack only the exact line range consumed, not a guessed byte offset.
    consumed = b"".join(lines[start:end])
    return records, {
        "start_line": window_start,
        "end_line": end,
        "next_line": end,
        "skipped_lines": skipped_lines,
        "start_prefix_sha256": (
            prefix_digest if window_start == start
            else hashlib.sha256(b"".join(lines[:window_start])).hexdigest()
        ),
        "prefix_sha256": hashlib.sha256(b"".join(lines[:end])).hexdigest(),
        "consumed_sha256": hashlib.sha256(consumed).hexdigest(),
        "log_sha256": hashlib.sha256(raw).hexdigest(),
        "warnings": warnings,
    }


def clean_event(event: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    allowed = {"op", "ts", "rev", "id", "item_id", "clip", "clip_id", "media_id", "track_id", "which", "kind", "duration", "in", "out", "new_version", "file", "name", "path", "preset", "from", "order", "text", "start", "end", "v", "speed", "gain", "x", "y", "scale", "rotate"}
    for key, value in event.items():
        if key not in allowed or contains_private_text(key):
            continue
        if isinstance(value, str):
            value = sanitize_public_text(value, limit=MAX_TEXT)
            if value is None:
                continue
        elif isinstance(value, list):
            value = [sanitize_public_text(v, limit=300) if isinstance(v, str) else v for v in value]
        elif not isinstance(value, (int, float, bool)) and value is not None:
            continue
        output[key] = value
    return output


def summarize(events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary: dict[str, Any] = {
        "new_materials": [], "revision_requests": [], "clip_trims": [], "exports": [],
        "timeline_changed": False, "presentation_changes": 0, "draft_messages": [],
        "meaningful_events": [],
    }
    property_last: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in events:
        event = clean_event(raw)
        op = str(event.get("op") or "").strip()
        if not op or op in NOISE_OPS:
            if op in NOISE_OPS:
                summary["presentation_changes"] += 1
            continue
        if op == "intent.message":
            if event.get("text"):
                summary["draft_messages"].append({"text": event["text"], "ts": event.get("ts")})
            continue
        if op == "asset.upload":
            summary["new_materials"].append({k: event[k] for k in ("id", "name", "path") if k in event})
        elif op == "clip.revision_requested":
            summary["revision_requests"].append({k: event[k] for k in ("clip", "text", "ts") if k in event})
        elif op in {"clip.trim", "trim"}:
            summary["clip_trims"].append({k: event[k] for k in ("clip", "in", "out", "new_version", "file") if k in event})
        elif op == "export.created":
            summary["exports"].append({k: event[k] for k in ("file", "preset", "from") if k in event})
        if op in {"assembly.reorder", "replace_timeline", "add", "remove", "split", "duplicate", "ripple_delete", "close_gap"}:
            summary["timeline_changed"] = True
        if op in PROPERTY_OPS:
            key = (str(event.get("item_id") or event.get("clip") or ""), str(event.get("which") or op))
            prior = property_last.get(key)
            if prior:
                prior["last"] = event
                prior["count"] = int(prior.get("count", 1)) + 1
            else:
                property_last[key] = {"first": event, "last": event, "count": 1}
        else:
            summary["meaningful_events"].append(event)
    summary["property_changes"] = list(property_last.values())
    # Keep context compact and deterministic.
    for key in ("new_materials", "revision_requests", "clip_trims", "exports", "draft_messages", "meaningful_events", "property_changes"):
        summary[key] = summary[key][-100:]
    return summary, events


def project_snapshot(project: Path) -> dict[str, Any]:
    try:
        document = json.loads((project / "project.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    asset = document.get("asset") if isinstance(document.get("asset"), dict) else {}
    assembly = document.get("assembly") if isinstance(document.get("assembly"), dict) else {}
    timeline = assembly.get("timeline") if isinstance(assembly.get("timeline"), dict) else {}
    tracks = timeline.get("tracks") if isinstance(timeline.get("tracks"), list) else []
    counts = {"video": 0, "audio": 0, "subtitle": 0}
    for track in tracks:
        if isinstance(track, dict):
            kind = str(track.get("kind") or "")
            if kind in counts:
                clips = track.get("clips") if isinstance(track.get("clips"), list) else track.get("cues") if isinstance(track.get("cues"), list) else []
                counts[kind] += len(clips)
    return {
        "title": sanitize_public_text(document.get("title"), limit=160),
        "revision": document.get("rev"),
        "status": document.get("status"),
        "asset_counts": {"scripts": len(asset.get("scripts", [])) if isinstance(asset.get("scripts"), list) else 0, "media": len(asset.get("media", [])) if isinstance(asset.get("media"), list) else 0, "clips": len(asset.get("clips", [])) if isinstance(asset.get("clips"), list) else 0, "finals": len(asset.get("finals", [])) if isinstance(asset.get("finals"), list) else 0},
        "timeline": {"video_items": counts["video"], "audio_items": counts["audio"], "subtitle_items": counts["subtitle"], "workspace_duration": timeline.get("workspace_duration"), "order": assembly.get("order") if isinstance(assembly.get("order"), list) else []},
    }


def read_context(root: Path, slug: str) -> dict[str, Any]:
    project = project_dir(root, slug)
    marker = read_marker(project)
    start = int(marker.get("next_line") or marker.get("line") or 0)
    events, cursor = log_records(project, start)
    summary, _ = summarize(events)
    has_delta = bool(events)
    return {"schema": 1, "project_slug": slug, "project": project_snapshot(project), "cursor": cursor, "has_delta": has_delta, "summary": summary, "notes_for_generator": ["Use the current project manifest as the source of truth.", "Draft messages are context only; do not execute them without a matching user request."], "ok": True}


def ack_context(root: Path, slug: str, cursor: dict[str, Any]) -> dict[str, Any]:
    project = project_dir(root, slug)
    if not isinstance(cursor, dict):
        raise ValueError("cursor must be an object")
    next_line = int(cursor.get("next_line", -1))
    expected = str(cursor.get("prefix_sha256") or "")
    if next_line < 0 or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("cursor is invalid")
    path = project / "logs" / "edits.log"
    raw = path.read_bytes() if path.is_file() else b""
    lines = raw.splitlines(keepends=True)
    if next_line > len(lines):
        raise ValueError("cursor is ahead of the current edit log")
    actual = hashlib.sha256(b"".join(lines[:next_line])).hexdigest()
    if actual != expected:
        raise ValueError("edit log changed before acknowledgement; reread context")
    current = read_marker(project)
    if int(current.get("next_line") or 0) > next_line:
        # The marker may already be ahead, but still validate the supplied
        # cursor against the current log before calling it idempotent.
        return {"ok": True, "idempotent": True, "next_line": int(current["next_line"])}
    atomic_json(marker_path(project), {"schema": 1, "next_line": next_line, "prefix_sha256": expected})
    return {"ok": True, "idempotent": int(current.get("next_line") or 0) == next_line, "next_line": next_line}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read or acknowledge manager edit context")
    parser.add_argument("command", choices=("read", "ack"))
    parser.add_argument("--root")
    parser.add_argument("--project", required=True)
    parser.add_argument("--cursor", help="JSON cursor returned by read")
    args = parser.parse_args(argv)
    try:
        root = resolve_root(args.root)
        result = read_context(root, args.project) if args.command == "read" else ack_context(root, args.project, json.loads(args.cursor or "{}"))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:240]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
