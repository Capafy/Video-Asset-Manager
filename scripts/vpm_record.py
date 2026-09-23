#!/usr/bin/env python3
"""Atomic bookkeeping helper for video-asset-manager."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from vpm_privacy import (  # type: ignore[import-not-found]
        ABSOLUTE_PATH_RE,
        PRIVATE_TEXT_RE,
        contains_private_text,
        redact_recursive,
        sanitize_public_text,
    )
except Exception:  # pragma: no cover - old copied bundle fallback
    ABSOLUTE_PATH_RE = re.compile(
        r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|users|tmp|var|private|root|workspace|mnt|opt|srv|etc|run|proc|sys)(?:[\\/]|$))",
        re.I,
    )
    PRIVATE_TEXT_RE = re.compile(
        r"(?:api[_-]?key|apikey|access[_-]?key|secret|password|authorization|bearer\s|"
        r"access[_-]?token|refresh[_-]?token|private[_-]?key|signed[_-]?url|"
        r"provider|model[_-]?route|stack\s*trace|x-amz-(?:credential|signature))",
        re.I,
    )

    def contains_private_text(value: object, *, allow_public_url: bool = False) -> bool:
        text = str(value or "")
        return bool(PRIVATE_TEXT_RE.search(text) or ABSOLUTE_PATH_RE.search(text)
                    or (not allow_public_url and re.search(r"(?:https?|s3|file|data|ftp)://|(?<!\w)//[^\s]+", text, re.I)))

    def sanitize_public_text(value: object, *, limit: int = 4000,
                             fallback: str | None = None,
                             allow_public_url: bool = False) -> str | None:
        text = str(value or "").replace("\x00", "").strip()
        return fallback if not text or len(text) > limit or contains_private_text(text, allow_public_url=allow_public_url) else text

    def redact_recursive(value: object, *, depth: int = 0) -> object:
        if depth > 12:
            return None
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if contains_private_text(key):
                    continue
                clean = redact_recursive(child, depth=depth + 1)
                if clean is not None:
                    result[str(key)] = clean
            return result
        if isinstance(value, list):
            return [clean for child in value if (clean := redact_recursive(child, depth=depth + 1)) is not None]
        if isinstance(value, str):
            return sanitize_public_text(value, limit=2_000_000)
        return value if isinstance(value, (int, float, bool)) or value is None else None


def default_root() -> str:
    explicit = os.environ.get("VIDEO_ASSET_MANAGER_ROOT") or os.environ.get("VPM_ROOT")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    # Capafy instances keep /home/user/workspace across resets, so prefer the team's
    # agreed projects path when it exists, or create it when its persistent parent
    # does.  Kept in lock-step with server.py so every entry point agrees.
    candidate = "/home/user/workspace/projects"
    parent = os.path.dirname(candidate)
    if os.path.isdir(candidate):
        return candidate
    if os.path.isdir(parent):
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            pass
    workspace = os.environ.get("CAPAFY_WORKSPACE")
    if workspace:
        return os.path.abspath(os.path.join(os.path.expanduser(workspace), ".capafy", "video-asset-manager"))
    return os.path.abspath(os.path.join(os.path.expanduser("~"), "workspace", ".capafy", "video-asset-manager"))


DEF_ROOT = default_root()
OUTCOMES = {"complete", "partial", "zero_replacement", "met", "partially_met", "missed"}
CLIP_ST = {"planned", "confirmed", "generating", "delivered", "revision_requested", "superseded"}
PROJECT_ST = {"draft", "planning", "generating", "reviewing", "done", "archived"}
MEDIA_KINDS = {"image", "video", "audio", "text", "font"}
MEDIA_ORIGINS = {"upload", "generated", "derived"}
SCRIPT_ROLES = {"script", "storyboard", "subtitle", "analysis_report"}
FINAL_KINDS = {"preview", "official", "export"}
# Keep the recorder's timeline contract aligned with the bundled manager
# server.  These constants are local on purpose: importing server.py from the
# bookkeeping CLI would couple atomic project writes to the HTTP runtime.
TIMELINE_TRACKS = {
    "video-main": "video",
    "video-overlay": "video",
    "video-2": "video",
    "video-3": "video",
    "audio-main": "audio",
    "text-main": "subtitle",
}
TIMELINE_RATIOS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}
TIMELINE_TRANSITIONS = {"cut", "fade", "dip_black"}
# Optional easing for the cross-dissolve (fade).  dip_black renders through
# ffmpeg's built-in fadeblack regardless; unknown values fall back to linear.
TIMELINE_EASINGS = {"linear", "ease_in", "ease_out", "ease_in_out"}
TIMELINE_MAX_TRACKS = 6
TIMELINE_MAX_ITEMS = 500
TIMELINE_MAX_CUES = 2000
TIMELINE_MAX_ZOOM = 64.0
TIMELINE_MIN_ZOOM = 0.25
TIMELINE_MAX_SECONDS = 24 * 60 * 60
DIRS = [
    "assets/uploads", "assets/generated", "assets/scripts", "assets/reports",
    "source/segments", "clips", "output/exports", "media/posters",
    "media/proxies", "media/filmstrips", "media/waveforms", "subtitles",
    "staging/generator", "tasks", "logs",
]
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,79}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$")
# Backwards-compatible names imported by the handoff adapter and older tools.
PRIVATE_RE = PRIVATE_TEXT_RE
ABSOLUTE_RE = ABSOLUTE_PATH_RE


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def public_label(value: Any, fallback: str) -> str:
    text = str(value or "").strip()[:80]
    if not text or contains_private_text(text):
        return fallback
    return text


def die(msg: str) -> None:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    raise SystemExit(1)


def jload(path: str | os.PathLike[str], default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def jwrite(path: str | os.PathLike[str], obj: Any) -> None:
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "\x1f".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def norm_rel(rel: Any) -> str | None:
    if not isinstance(rel, str):
        return None
    value = rel.strip().replace("\\", "/")
    if "\x00" in value:
        return None
    if not value or value.startswith(("/", "//", "data:", "http:", "https:")):
        return None
    if re.match(r"^[A-Za-z]:", value):
        return None
    parts = [part for part in value.split("/") if part not in ("", ".")]
    devices = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if not parts or any(part == ".." or ":" in part or part.upper().split(".", 1)[0] in devices for part in parts):
        return None
    return "/".join(parts)


def path_within(base: str, candidate: str) -> bool:
    try:
        base_real = os.path.normcase(os.path.realpath(base))
        candidate_real = os.path.normcase(os.path.realpath(candidate))
        return os.path.commonpath((base_real, candidate_real)) == base_real
    except (OSError, ValueError):
        return False


def public_text(value: Any, label: str = "value") -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if contains_private_text(text):
        die(f"{label} contains private or internal data")
    return text


def public_id(value: Any, label: str) -> str:
    ident = str(value or "").strip()
    if not ID_RE.fullmatch(ident) or PRIVATE_RE.search(ident):
        die(f"{label} contains an invalid id")
    return ident


_DROP = object()


def sanitize_public(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if contains_private_text(key):
                continue
            sanitized = sanitize_public(child)
            if sanitized is not _DROP:
                clean[key] = sanitized
        return clean
    if isinstance(value, list):
        clean_list = []
        for child in value:
            sanitized = sanitize_public(child)
            if sanitized is not _DROP:
                clean_list.append(sanitized)
        return clean_list
    if isinstance(value, str) and contains_private_text(value):
        return _DROP
    return value


def empty_project(slug: str = "", title: str = "", ptype: str = "clone", ratio: str = "9:16") -> dict[str, Any]:
    return {
        "schema": 2, "rev": 0, "slug": slug, "title": title, "type": ptype,
        "status": "draft", "created": now(), "updated": now(),
        "presets": {"ratio": ratio, "clarity": "Standard"},
        "source": {"file": None, "origin_url": None, "duration": None, "segments": [], "analysis": None},
        "asset": {"scripts": [], "media": [], "clips": [], "finals": []},
        "assets": [], "clips": [],
        "assembly": {
            "order": [], "transition": "cut",
            "audio": {"bgm": None, "bgm_gain": 0, "mute_original": False},
            "subtitles": {"file": None, "burn": False},
            "preview": None, "official": None, "exports": [],
        },
    }


def _dedupe(entries: Any, prefix: str = "item") -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_files: set[str] = set()
    if not isinstance(entries, list):
        return result
    for item in entries:
        if not isinstance(item, dict):
            continue
        copied = copy.deepcopy(item)
        ident = str(copied.get("id") or "").strip()
        if not ident:
            ident = stable_id(prefix, copied.get("file") or copied.get("path"), copied.get("name"), copied.get("created"))
            copied["id"] = ident
        file_value = norm_rel(copied.get("file") or copied.get("path"))
        if ident not in seen and (not file_value or file_value not in seen_files):
            seen.add(ident)
            if file_value:
                seen_files.add(file_value)
            result.append(copied)
    return result


def normalize_project(raw: Any, slug: str | None = None) -> dict[str, Any]:
    p = copy.deepcopy(raw) if isinstance(raw, dict) else empty_project()
    if slug:
        p["slug"] = slug
    p.setdefault("title", p.get("slug") or "未命名项目")
    p.setdefault("type", "clone")
    p.setdefault("status", "draft")
    p.setdefault("created", now())
    p.setdefault("updated", now())
    p.setdefault("presets", {"ratio": "9:16", "clarity": "Standard"})
    source = p.get("source") if isinstance(p.get("source"), dict) else {}
    source.setdefault("file", None)
    source.setdefault("origin_url", None)
    source.setdefault("duration", None)
    source.setdefault("segments", [])
    source.setdefault("analysis", None)
    p["source"] = source
    legacy_media = _dedupe(p.get("assets"), "as")
    legacy_clips = _dedupe(p.get("clips"), "clip")
    legacy_assembly = p.get("assembly") if isinstance(p.get("assembly"), dict) else {}
    canonical = p.get("asset") if isinstance(p.get("asset"), dict) else {}
    scripts = _dedupe(canonical.get("scripts"), "scr")
    media = _dedupe(canonical.get("media"), "as")
    clips = _dedupe(canonical.get("clips"), "clip")
    finals = _dedupe(canonical.get("finals"), "final")
    script_ids = {str(item.get("id")) for item in scripts}
    media_ids = {str(item.get("id")) for item in media}
    def has_record(entries: list[dict[str, Any]], item: dict[str, Any]) -> bool:
        ident = str(item.get("id") or "")
        rel = norm_rel(item.get("file") or item.get("path"))
        return any(str(existing.get("id") or "") == ident or
                   (rel and norm_rel(existing.get("file") or existing.get("path")) == rel)
                   for existing in entries)

    for item in legacy_media:
        role = str(item.get("role") or item.get("kind") or "").lower()
        rel = str(item.get("file") or "").replace("\\", "/")
        if role in SCRIPT_ROLES or rel.startswith(("assets/scripts/", "assets/reports/", "subtitles/")):
            if not has_record(scripts, item):
                item.setdefault("role", role if role in SCRIPT_ROLES else "script")
                scripts.append(item)
                script_ids.add(str(item.get("id")))
        elif not has_record(media, item):
            media.append(item)
            media_ids.add(str(item.get("id")))
    clip_ids = {str(item.get("id")) for item in clips}
    for item in legacy_clips:
        if not has_record(clips, item):
            clips.append(item)
            clip_ids.add(str(item.get("id")))
    order = list(legacy_assembly.get("order") or [])
    final_ids = {str(item.get("id")) for item in finals}
    official = legacy_assembly.get("official")
    preview = legacy_assembly.get("preview")
    if official and "final_official" not in final_ids:
        finals.append({"id": "final_official", "kind": "official", "file": official, "name": "Official final",
                       "preset": None, "from": order, "created": p.get("updated") or now(), "status": "active"})
        final_ids.add("final_official")
    if preview and "final_preview" not in final_ids:
        finals.append({"id": "final_preview", "kind": "preview", "file": preview, "name": "Assembly preview",
                       "preset": None, "from": order, "created": p.get("updated") or now(), "status": "active"})
        final_ids.add("final_preview")
    for item in legacy_assembly.get("exports") or []:
        if not isinstance(item, dict) or not item.get("file"):
            continue
        fid = stable_id("final", item.get("file"), item.get("preset"), item.get("created"))
        if fid not in final_ids:
            finals.append({"id": fid, "kind": "export", "file": item["file"], "name": item.get("preset") or "Export",
                           "preset": item.get("preset"), "from": order, "created": item.get("created") or p.get("updated") or now(),
                           "status": "active"})
            final_ids.add(fid)
    p["schema"] = 2
    p["asset"] = {"scripts": scripts, "media": media, "clips": clips, "finals": finals}
    p["assets"] = media
    p["clips"] = clips
    assembly = copy.deepcopy(legacy_assembly)
    if not isinstance(assembly.get("order"), list):
        assembly["order"] = order
    if not isinstance(assembly.get("exports"), list):
        assembly["exports"] = []
    assembly.setdefault("order", order)
    assembly.setdefault("transition", "cut")
    assembly.setdefault("audio", {"bgm": None, "bgm_gain": 0, "mute_original": False})
    assembly.setdefault("subtitles", {"file": None, "burn": False})
    assembly.setdefault("preview", next((item.get("file") for item in finals if item.get("kind") == "preview"), None))
    assembly.setdefault("official", next((item.get("file") for item in finals if item.get("kind") == "official"), None))
    assembly.setdefault("exports", [])
    known_exports = {str(item.get("file")) for item in assembly["exports"] if isinstance(item, dict)}
    for item in finals:
        if item.get("kind") == "export" and item.get("file") and str(item["file"]) not in known_exports:
            assembly["exports"].append({"file": item["file"], "preset": item.get("preset") or item.get("name") or "export",
                                        "created": item.get("created") or now(), "from": "assembly"})
    p["assembly"] = assembly
    return p


def collect_paths(doc: Any) -> list[str]:
    paths: list[str] = []
    path_keys = {"file", "path", "poster", "proxy", "filmstrip", "sidecar", "analysis", "preview", "official"}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, str) and (key in path_keys or key.endswith("_file")):
                    rel = norm_rel(child)
                    if rel:
                        paths.append(rel)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(doc)
    return paths


def _finite_number(value: Any, default: float | None = None) -> float | None:
    """Return a finite float, preserving ``None`` for malformed values."""

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def timeline_issues(doc: dict[str, Any]) -> list[str]:
    """Validate the optional ``assembly.timeline`` edit model.

    The HTTP server performs the same normalization for browser writes, but
    recorder-driven writes and handoff repairs also need a standalone guard.
    Return human-readable issue strings instead of raising so ``check`` can
    report all findings in one pass.
    """

    assembly = doc.get("assembly") if isinstance(doc.get("assembly"), dict) else {}
    timeline = assembly.get("timeline")
    if timeline is None:
        return []
    if not isinstance(timeline, dict):
        return ["assembly.timeline must be an object"]

    issues: list[str] = []
    if "schema" in timeline and timeline.get("schema") not in (2, "2"):
        issues.append("timeline schema must be 2")

    canvas = timeline.get("canvas")
    if canvas is not None:
        if not isinstance(canvas, dict):
            issues.append("timeline canvas must be an object")
        else:
            ratio = str(canvas.get("ratio") or "")
            if ratio not in TIMELINE_RATIOS:
                issues.append("timeline canvas has an invalid ratio")
            for key in ("width", "height"):
                value = _finite_number(canvas.get(key))
                if value is None or value < 64 or value > 4096 or int(value) != value:
                    issues.append(f"timeline canvas has an invalid {key}")
    zoom = _finite_number(timeline.get("zoom"), 1.0)
    if zoom is None or zoom < TIMELINE_MIN_ZOOM or zoom > TIMELINE_MAX_ZOOM:
        issues.append("timeline zoom is out of range")
    if "snap" in timeline and not isinstance(timeline.get("snap"), bool):
        issues.append("timeline snap must be boolean")

    # Keep this helper total for callers that use it before the outer manifest
    # validator has checked the legacy projections.  Malformed collections are
    # treated as empty here and reported as missing references below rather
    # than raising an incidental AttributeError.
    clip_values = doc.get("clips") if isinstance(doc.get("clips"), list) else []
    asset_value = doc.get("asset") if isinstance(doc.get("asset"), dict) else {}
    media_values = asset_value.get("media") if isinstance(asset_value.get("media"), list) else []
    clips = {
        str(item.get("id")): item
        for item in clip_values
        if isinstance(item, dict) and ID_RE.fullmatch(str(item.get("id") or ""))
    }
    media = {
        str(item.get("id")): item
        for item in media_values
        if isinstance(item, dict) and ID_RE.fullmatch(str(item.get("id") or ""))
    }
    tracks = timeline.get("tracks")
    if not isinstance(tracks, list):
        return issues + ["timeline tracks must be a list"]
    if len(tracks) != len(TIMELINE_TRACKS):
        issues.append("timeline must contain exactly six tracks")
    seen_tracks: set[str] = set()
    seen_items: set[str] = set()
    primary_order: list[str] = []
    video_track_items: dict[str, list[dict[str, Any]]] = {}

    def check_style(item_id: str, style: Any) -> None:
        if style is None:
            return
        if not isinstance(style, dict):
            issues.append(f"subtitle cue {item_id}: invalid style")
            return
        # Keep the legacy handoff vocabulary while accepting the Studio's
        # canonical style fields.  The server normalizes the aliases on save;
        # the recorder must not reject an otherwise valid Studio project.
        allowed = {
            "font", "size", "fontSize", "color", "primary", "stroke",
            "outlineColor", "outlineWidth", "background", "backgroundOpacity",
            "position", "align",
        }
        for key in style:
            if str(key) not in allowed:
                issues.append(f"subtitle cue {item_id}: unsupported style field")
        for key, value in style.items():
            if str(key) not in allowed:
                continue
            key = str(key)
            if isinstance(value, bool):
                issues.append(f"subtitle cue {item_id}: invalid style value")
            elif isinstance(value, (int, float)):
                number = _finite_number(value)
                if number is None or number < -10000 or number > 10000:
                    issues.append(f"subtitle cue {item_id}: invalid style value")
                elif key in {"size", "fontSize"} and (number <= 0 or number > 256):
                    issues.append(f"subtitle cue {item_id}: invalid font size")
                elif key in {"outlineWidth", "stroke"} and (number < 0 or number > 12):
                    issues.append(f"subtitle cue {item_id}: invalid outline width")
                elif key == "backgroundOpacity" and (number < 0 or number > 1):
                    issues.append(f"subtitle cue {item_id}: invalid background opacity")
            elif isinstance(value, str):
                clean = value.strip()
                if not clean or len(clean) > 120 or contains_private_text(clean):
                    issues.append(f"subtitle cue {item_id}: invalid style value")
                elif key in {"color", "primary", "stroke", "outlineColor", "background"} and not re.fullmatch(
                    r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", clean
                ):
                    issues.append(f"subtitle cue {item_id}: invalid color")
                elif key == "position" and clean.lower() not in {"top", "center", "bottom"}:
                    issues.append(f"subtitle cue {item_id}: invalid position")
                elif key == "align" and clean.lower() not in {"left", "center", "right"}:
                    issues.append(f"subtitle cue {item_id}: invalid alignment")
                elif key == "font" and len(clean) > 40:
                    issues.append(f"subtitle cue {item_id}: invalid font")
            else:
                issues.append(f"subtitle cue {item_id}: invalid style value")

    for track in tracks:
        if not isinstance(track, dict):
            issues.append("timeline track must be an object")
            continue
        track_id = str(track.get("id") or "")
        kind = str(track.get("kind") or "").lower()
        if track_id not in TIMELINE_TRACKS:
            issues.append(f"unknown timeline track {track_id or '<empty>'}")
            continue
        if track_id in seen_tracks:
            issues.append(f"duplicate timeline track {track_id}")
            continue
        seen_tracks.add(track_id)
        expected_kind = TIMELINE_TRACKS[track_id]
        if kind != expected_kind:
            issues.append(f"timeline track {track_id} has invalid kind")
        if expected_kind == "subtitle":
            cues = track.get("cues")
            if not isinstance(cues, list):
                issues.append(f"timeline track {track_id} cues must be a list")
                continue
            if len(cues) > TIMELINE_MAX_CUES:
                issues.append(f"timeline track {track_id} has too many cues")
            cue_ids: set[str] = set()
            for cue in cues[:TIMELINE_MAX_CUES]:
                if not isinstance(cue, dict) or not ID_RE.fullmatch(str(cue.get("id") or "")):
                    issues.append("invalid subtitle cue id")
                    continue
                cue_id = str(cue["id"])
                if cue_id in cue_ids:
                    issues.append(f"duplicate subtitle cue {cue_id}")
                cue_ids.add(cue_id)
                start = _finite_number(cue.get("start"))
                end = _finite_number(cue.get("end"))
                text = str(cue.get("text") or "").strip()
                if start is None or end is None or start < 0 or end <= start or end > TIMELINE_MAX_SECONDS:
                    issues.append(f"subtitle cue {cue_id}: invalid range")
                if not text or len(text) > 2000 or contains_private_text(text):
                    issues.append(f"subtitle cue {cue_id}: invalid text")
                check_style(cue_id, cue.get("style"))
            continue

        values = track.get("clips")
        if not isinstance(values, list):
            issues.append(f"timeline track {track_id} clips must be a list")
            continue
        if len(values) > TIMELINE_MAX_ITEMS:
            issues.append(f"timeline track {track_id} has too many items")
        track_items: list[dict[str, Any]] = []
        for item in values[:TIMELINE_MAX_ITEMS]:
            if not isinstance(item, dict) or not ID_RE.fullmatch(str(item.get("id") or "")):
                issues.append(f"timeline track {track_id}: invalid item id")
                continue
            item_id = str(item["id"])
            if item_id in seen_items:
                issues.append(f"duplicate timeline item {item_id}")
            seen_items.add(item_id)
            track_items.append(item)

            clip_id = str(item.get("clip_id") or "").strip()
            media_id = str(item.get("media_id") or "").strip()
            if expected_kind == "video":
                if not clip_id or clip_id not in clips:
                    issues.append(f"timeline item {item_id}: video clip reference is missing")
                if media_id:
                    issues.append(f"timeline item {item_id}: video item cannot use media_id")
                if track_id == "video-main" and clip_id:
                    primary_order.append(clip_id)
            else:
                if clip_id and media_id:
                    issues.append(f"timeline item {item_id}: audio item cannot contain both references")
                if clip_id and clip_id not in clips:
                    issues.append(f"timeline item {item_id}: unknown clip reference")
                if media_id:
                    referenced = media.get(media_id)
                    if referenced is None or str(referenced.get("kind") or "").lower() not in {"audio", "video"}:
                        issues.append(f"timeline item {item_id}: invalid media reference")
                if not clip_id and not media_id:
                    issues.append(f"timeline item {item_id}: audio reference is missing")

            version = item.get("version")
            if clip_id and version is not None:
                clip = clips.get(clip_id) or {}
                versions = clip.get("versions") if isinstance(clip, dict) else None
                try:
                    version_number = int(version)
                except (TypeError, ValueError, OverflowError):
                    version_number = None
                if version_number is None or not isinstance(versions, list) or not any(
                        isinstance(value, dict) and value.get("v") == version_number for value in versions):
                    issues.append(f"timeline item {item_id}: media version not found")

            for key in ("start", "in", "out", "duration", "speed"):
                value = _finite_number(item.get(key))
                if value is None or value < 0 or value > TIMELINE_MAX_SECONDS:
                    issues.append(f"timeline item {item_id}: invalid {key}")
            in_point = _finite_number(item.get("in"))
            out_point = _finite_number(item.get("out"))
            duration = _finite_number(item.get("duration"))
            speed = _finite_number(item.get("speed"))
            if in_point is not None and out_point is not None and out_point <= in_point:
                issues.append(f"timeline item {item_id}: out must be greater than in")
            if duration is not None and duration <= 0:
                issues.append(f"timeline item {item_id}: duration must be positive")
            if speed is not None and (speed < 0.25 or speed > 4.0):
                issues.append(f"timeline item {item_id}: speed is out of range")

            transform = item.get("transform")
            if transform is not None:
                if not isinstance(transform, dict):
                    issues.append(f"timeline item {item_id}: invalid transform")
                else:
                    for key in ("x", "y", "scale", "rotate", "opacity", "border"):
                        value = _finite_number(transform.get(key))
                        if value is None:
                            issues.append(f"timeline item {item_id}: invalid transform {key}")
                    scale = _finite_number(transform.get("scale"))
                    if scale is not None and (scale < 0.05 or scale > 20):
                        issues.append(f"timeline item {item_id}: transform scale is out of range")
                    opacity = _finite_number(transform.get("opacity"))
                    if opacity is not None and (opacity < 0.05 or opacity > 1.0):
                        issues.append(f"timeline item {item_id}: transform opacity is out of range")
                    border = _finite_number(transform.get("border"))
                    if border is not None and (border < 0.0 or border > 12.0):
                        issues.append(f"timeline item {item_id}: transform border is out of range")

            for key, lower, upper in (("gain", -60.0, 24.0), ("fade_in", 0.0, 30.0), ("fade_out", 0.0, 30.0)):
                if key in item:
                    value = _finite_number(item.get(key))
                    if value is None or value < lower or value > upper:
                        issues.append(f"timeline item {item_id}: invalid {key}")
            if "detach" in item and not isinstance(item.get("detach"), bool):
                issues.append(f"timeline item {item_id}: detach must be boolean")
            if duration is not None:
                for key in ("fade_in", "fade_out"):
                    fade = _finite_number(item.get(key), 0.0)
                    if fade is not None and fade > duration:
                        issues.append(f"timeline item {item_id}: {key} exceeds item duration")

        video_track_items[track_id] = track_items

    missing_tracks = set(TIMELINE_TRACKS) - seen_tracks
    for track_id in sorted(missing_tracks):
        issues.append(f"missing timeline track {track_id}")

    # A lane has one-dimensional occupancy.  Overlay clips may overlap the
    # primary track, but clips on the same lane must not overlap each other.
    for track_id, values in video_track_items.items():
        if TIMELINE_TRACKS.get(track_id) not in {"video", "audio"}:
            continue
        ordered = sorted(values, key=lambda value: (_finite_number(value.get("start"), 0.0), str(value.get("id"))))
        previous_end = None
        previous_id = None
        for item in ordered:
            start = _finite_number(item.get("start"))
            duration = _finite_number(item.get("duration"))
            if start is None or duration is None:
                continue
            if previous_end is not None and start < previous_end - 0.000001:
                issues.append(f"timeline track {track_id}: items {previous_id} and {item.get('id')} overlap")
            end = start + duration
            if previous_end is None or end > previous_end:
                previous_end = end
                previous_id = item.get("id")

    # Fades are cross-dissolves, not edge effects.  Require a neighboring item
    # on the same video lane and bound the duration by both usable clips.
    for track_id, values in video_track_items.items():
        if TIMELINE_TRACKS.get(track_id) != "video":
            continue
        ordered = sorted(values, key=lambda value: (_finite_number(value.get("start"), 0.0), str(value.get("id"))))
        for index, item in enumerate(ordered):
            item_id = str(item.get("id"))
            for edge in ("in", "out"):
                transition = item.get(f"transition_{edge}")
                if transition is None:
                    continue
                if not isinstance(transition, dict):
                    issues.append(f"timeline item {item_id}: invalid transition_{edge}")
                    continue
                kind = str(transition.get("kind") or "cut").lower()
                amount = _finite_number(transition.get("duration"), 0.0)
                if kind not in TIMELINE_TRANSITIONS:
                    issues.append(f"timeline item {item_id}: invalid transition_{edge} kind")
                    continue
                easing = str(transition.get("easing") or "linear").lower()
                if easing not in TIMELINE_EASINGS:
                    issues.append(f"timeline item {item_id}: invalid transition_{edge} easing")
                    continue
                if amount is None or amount < 0 or amount > 2:
                    issues.append(f"timeline item {item_id}: invalid transition_{edge} duration")
                    continue
                if kind == "cut":
                    if amount > 0.000001:
                        issues.append(f"timeline item {item_id}: cut transition duration must be zero")
                    continue
                neighbor = ordered[index - 1] if edge == "in" and index > 0 else (
                    ordered[index + 1] if edge == "out" and index + 1 < len(ordered) else None
                )
                if neighbor is None:
                    issues.append(f"timeline item {item_id}: edge fade requires an adjacent clip")
                    continue
                start = _finite_number(item.get("start"), 0.0)
                duration = _finite_number(item.get("duration"), 0.0)
                neighbor_start = _finite_number(neighbor.get("start"), 0.0)
                neighbor_duration = _finite_number(neighbor.get("duration"), 0.0)
                if edge == "out":
                    adjacent = abs(neighbor_start - (start + duration)) <= 0.01
                else:
                    adjacent = abs(start - (neighbor_start + neighbor_duration)) <= 0.01
                if not adjacent:
                    issues.append(f"timeline item {item_id}: fade requires adjacent clips")
                    continue
                usable = min(2.0, duration / 2.0, neighbor_duration / 2.0)
                if amount <= 0 or amount > usable + 0.000001:
                    issues.append(f"timeline item {item_id}: fade duration exceeds usable clip duration")

    if "video-main" in seen_tracks and isinstance(assembly.get("order"), list):
        order = [str(value) for value in assembly.get("order", [])]
        if order != primary_order:
            issues.append("assembly.order is out of sync with video-main timeline")
    return issues


def validate_project(doc: dict[str, Any], root_dir: str | None = None) -> None:
    if not isinstance(doc, dict) or not SLUG_RE.match(str(doc.get("slug") or "")):
        die("invalid project manifest")
    if doc.get("status") not in PROJECT_ST:
        die("invalid project status")
    asset = doc.get("asset")
    if not isinstance(asset, dict):
        die("asset groups are missing")
    for key in ("scripts", "media", "clips", "finals"):
        if not isinstance(asset.get(key), list):
            die(f"asset.{key} must be a list")
    if not isinstance(doc.get("assets"), list) or not isinstance(doc.get("clips"), list):
        die("legacy projections are missing")
    if asset["media"] != doc["assets"] or asset["clips"] != doc["clips"]:
        die("legacy projection is out of sync")
    for group in ("scripts", "media", "clips", "finals"):
        for item in asset[group]:
            if not isinstance(item, dict) or not ID_RE.fullmatch(str(item.get("id") or "")):
                die(f"invalid id in asset.{group}")
    path_keys = {"file", "path", "poster", "proxy", "filmstrip", "sidecar", "analysis", "preview", "official"}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if contains_private_text(str(key)):
                    die("project contains a private field")
                if isinstance(child, str) and (key in path_keys or key.endswith("_file")) and child and norm_rel(child) is None:
                    die(f"invalid project-relative path in {key}")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and contains_private_text(value):
            die("project contains private or internal data")

    walk(doc)
    timeline_errors = timeline_issues(doc)
    if timeline_errors:
        die(timeline_errors[0])
    if root_dir:
        base = os.path.realpath(root_dir)
        for rel in collect_paths(doc):
            target = os.path.realpath(os.path.join(base, rel))
            if not path_within(base, target):
                die("project path escapes project root")


def sync_index(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for entry in sorted(os.listdir(root)):
        if entry in {"webapp", "trash"} or not SLUG_RE.match(entry):
            continue
        project_dir = os.path.join(root, entry)
        if os.path.islink(project_dir) or not os.path.isdir(project_dir) or not path_within(root, project_dir):
            continue
        project_file = os.path.join(project_dir, "project.json")
        # Never follow a manifest link while rebuilding the root projection.
        # A project manifest is customer-visible data and therefore also a
        # security boundary: an external symlink/junction must not make its
        # contents appear in index.json.  Atomic writes replace the regular
        # file and remain compatible with this check.
        if (os.path.islink(project_file) or not os.path.isfile(project_file)
                or not path_within(project_dir, project_file)):
            continue
        document = normalize_project(jload(project_file, {}), entry)
        clips = document.get("clips", [])
        cover = next((entry + "/" + str(c["poster"]) for c in clips if c.get("poster") and norm_rel(c.get("poster"))), None)
        size = 0
        for base, dirs, files in os.walk(project_dir):
            staging_root = os.path.realpath(os.path.join(project_dir, "staging"))
            real_base = os.path.realpath(base)
            if real_base == staging_root or real_base.startswith(staging_root + os.sep):
                dirs[:] = []
                continue
            for name in files:
                try:
                    candidate = os.path.join(base, name)
                    # A linked file can point outside the portable project;
                    # omit it from the projection size rather than following
                    # the target.
                    if os.path.islink(candidate) or not path_within(project_dir, candidate):
                        continue
                    size += os.path.getsize(candidate)
                except OSError:
                    pass
        rows.append({
            "slug": entry,
            "title": public_label(document.get("title"), entry),
            "type": document.get("type"),
            "status": document.get("status"),
            "cover": cover,
            "clips_done": sum(1 for c in clips if c.get("status") == "delivered"),
            "clips_total": len(clips),
            "finals_done": sum(1 for f in document["asset"]["finals"] if f.get("status", "active") == "active"),
            "updated": document.get("updated") or "",
            "size_bytes": size,
        })
    rows.sort(key=lambda row: row["updated"], reverse=True)
    jwrite(os.path.join(root, "index.json"), {"schema": 1, "updated": now(), "projects": rows})


def ffmpeg_ok() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_media_duration(path: str | os.PathLike[str]) -> float | None:
    """Return media duration when ffprobe is available, without leaking output."""

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", os.fspath(path)],
            capture_output=True, text=True, timeout=30,
        )
        value = float((result.stdout or "").strip())
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return None
    return value if result.returncode == 0 and math.isfinite(value) and value > 0 else None


def derivatives(project: "P", clip_id: str, source_rel: str) -> dict[str, str]:
    if not ffmpeg_ok():
        return {}
    source = os.path.join(project.dir, source_rel)
    if not os.path.isfile(source):
        return {}
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", clip_id)
    # Keep the static strip fully populated.  The old fixed tile=10x1 left
    # black cells whenever a short clip yielded fewer than ten samples.
    duration = probe_media_duration(source) or 4.0
    strip_cols = max(8, min(48, int(math.ceil(duration * 8))))
    jobs = [
        ("poster", f"media/posters/{safe_id}.jpg", ["-ss", "0.5", "-i", source, "-frames:v", "1", "-vf", "scale=640:-2"]),
        ("proxy", f"media/proxies/{safe_id}.mp4", ["-i", source, "-vf", "scale=480:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "64k"]),
        ("filmstrip", f"media/filmstrips/{safe_id}.jpg", ["-i", source, "-vf", f"fps=8,scale=160:-2:flags=lanczos,tile={strip_cols}x1:padding=0:margin=0", "-frames:v", "1"]),
    ]
    outputs: dict[str, str] = {}
    for key, rel, args in jobs:
        destination = os.path.join(project.dir, rel)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            result = subprocess.run(["ffmpeg", "-y", *args, destination], capture_output=True, timeout=180)
            if result.returncode == 0 and os.path.exists(destination):
                outputs[key] = rel
        except (subprocess.TimeoutExpired, OSError):
            pass
    return outputs


class P:
    def __init__(self, root: str, slug: str):
        if not SLUG_RE.match(slug) or ".." in slug:
            die(f"bad slug: {slug}")
        self.root = os.path.abspath(root)
        self.slug = slug
        self.dir = os.path.join(self.root, slug)
        self.pj = os.path.join(self.dir, "project.json")
        if os.path.lexists(self.dir) and (os.path.islink(self.dir) or not path_within(self.root, self.dir)):
            die("project directory escapes workspace")

    def load(self) -> dict[str, Any]:
        document = jload(self.pj)
        if document is None:
            die(f"no such project: {self.slug}")
        return normalize_project(document, self.slug)

    def rel_ok(self, rel: Any) -> bool:
        value = norm_rel(rel)
        if not value:
            return False
        if os.path.islink(self.dir) or not os.path.isdir(self.dir) or not path_within(self.root, self.dir):
            return False
        target = os.path.realpath(os.path.join(self.dir, value))
        return path_within(self.dir, target)

    def save(self, document: dict[str, Any], event: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        document = normalize_project(document, self.slug)
        validate_project(document, self.dir)
        document["rev"] = int(document.get("rev", 0)) + 1
        document["updated"] = now()
        jwrite(self.pj, document)
        line = {"event": event, "ts": document["updated"], "rev": document["rev"]}
        if extra:
            line.update({key: value for key, value in extra.items()
                         if not contains_private_text(str(key)) and not contains_private_text(str(value))})
        os.makedirs(os.path.join(self.dir, "logs"), exist_ok=True)
        with open(os.path.join(self.dir, "logs", "timeline.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        sync_index(self.root)
        return document

    def state(self, **updates: Any) -> None:
        state_file = os.path.join(self.dir, "state.json")
        state = jload(state_file, {"schema": 1, "rev": 0, "phase": "draft", "active_clip": None,
                                   "queue": [], "last_error": {"clip": None, "message": None}})
        state = sanitize_public(state)
        if not isinstance(state, dict):
            state = {"schema": 1, "rev": 0, "phase": "draft", "active_clip": None,
                     "queue": [], "last_error": {"clip": None, "message": None}}
        state["rev"] = int(state.get("rev", 0)) + 1
        for key, value in updates.items():
            if contains_private_text(str(key)):
                continue
            sanitized = sanitize_public(value)
            if sanitized is not _DROP:
                state[key] = sanitized
        state["updated"] = now()
        if isinstance(state.get("last_error"), dict) and (
                contains_private_text(str(state["last_error"].get("message") or ""))):
            state["last_error"] = {"clip": None, "message": "发生了一个无法公开的错误。"}
        jwrite(state_file, state)

    def clip(self, document: dict[str, Any], clip_id: str) -> dict[str, Any]:
        for clip in document["clips"]:
            if clip.get("id") == clip_id:
                return clip
        die(f"no such clip: {clip_id}")


def ensure_dirs(project_dir: str) -> None:
    for rel in DIRS:
        os.makedirs(os.path.join(project_dir, rel), exist_ok=True)


def cmd_create(args: argparse.Namespace) -> None:
    os.makedirs(args.root, exist_ok=True)
    base = re.sub(r"[^A-Za-z0-9\-]+", "-", args.slug or args.title).strip("-")[:40] or "project"
    slug = args.slug or f"{time.strftime('%Y-%m-%d')}-{base}"
    index = 0
    while os.path.exists(os.path.join(args.root, slug)) and index < 1000:
        index += 1
        slug = f"{time.strftime('%Y-%m-%d')}-{base}-{index}"
    if not SLUG_RE.match(slug):
        die("slug generation failed")
    project = P(args.root, slug)
    ensure_dirs(project.dir)
    document = empty_project(slug, public_text(args.title, "title") or "未命名项目", args.type, args.ratio)
    jwrite(project.pj, document)
    project.state(phase="draft")
    project.save(document, "project.created", {"title": document["title"]})
    Path(os.path.join(args.root, "active_project")).write_text(slug, encoding="utf-8")
    print(json.dumps({"ok": True, "slug": slug}, ensure_ascii=False))


def cmd_segments(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    segments = jload(args.file)
    if not isinstance(segments, list):
        die("segments --file must be a JSON list")
    for segment in segments:
        if not isinstance(segment, dict):
            die("each segment must be an object")
        if segment.get("file") and not norm_rel(segment["file"]):
            die("segment file must be project-relative")
    document["source"]["segments"] = segments
    if args.source_file:
        if not project.rel_ok(args.source_file):
            die("source file must be inside project")
        document["source"]["file"] = norm_rel(args.source_file)
    if args.duration is not None:
        document["source"]["duration"] = args.duration
    if document["status"] == "draft":
        document["status"] = "planning"
    project.save(document, "source.segmented", {"count": len(segments)})
    project.state(phase="planning")
    print(json.dumps({"ok": True, "segments": len(segments)}))


def cmd_plan(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    args.clip = public_id(args.clip, "clip")
    existing = next((clip for clip in document["clips"] if clip.get("id") == args.clip), None)
    entry = existing or {
        "id": args.clip, "versions": [], "current": None, "poster": None,
        "proxy": None, "filmstrip": None, "revision_note": None, "outcome": None,
        "handoff_status": None, "handoff_message": None,
        "handoff_task_id": None, "handoff_updated": None,
    }
    entry.update({
        "segment": args.segment,
        "title": public_text(args.title or entry.get("title") or args.clip, "title"),
        "duration": args.duration,
        "ratio": args.ratio,
        "clarity": args.clarity,
        "mappings": [public_text(item, "mapping") for item in (args.mapping or entry.get("mappings") or [])],
        "preserve": public_text(args.preserve or entry.get("preserve"), "preserve"),
        "status": "planned",
        "handoff_status": None,
        "handoff_message": None,
        "handoff_task_id": None,
        "handoff_updated": None,
        "revision_note": None,
    })
    # Keep the optional handoff projection present on manually planned clips
    # as well, so a later generator failure can update the same row without
    # changing the schema shape seen by the web application.
    entry.setdefault("handoff_status", None)
    entry.setdefault("handoff_message", None)
    entry.setdefault("handoff_task_id", None)
    entry.setdefault("handoff_updated", None)
    if not existing:
        document["clips"].append(entry)
    if document["status"] == "draft":
        document["status"] = "planning"
    project.save(document, "clip.planned", {"clip": args.clip})
    project.state(phase="planning")
    print(json.dumps({"ok": True, "clip": args.clip, "status": "planned"}, ensure_ascii=False))


def cmd_confirm(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    ids = [public_id(item, "clip") for item in args.clip] if args.clip else [clip["id"] for clip in document["clips"] if clip.get("status") == "planned"]
    for clip_id in ids:
        project.clip(document, clip_id)["status"] = "confirmed"
    project.save(document, "plan.confirmed", {"clips": ids})
    print(json.dumps({"ok": True, "confirmed": ids}))


def cmd_start(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    args.clip = public_id(args.clip, "clip")
    project.clip(document, args.clip)["status"] = "generating"
    document["status"] = "generating"
    project.save(document, "clip.generating", {"clip": args.clip})
    project.state(phase="generating", active_clip=args.clip,
                  queue=[{"clip": args.clip, "status": "generating", "started": now()}])
    print(json.dumps({"ok": True, "clip": args.clip, "status": "generating"}))


def cmd_delivery(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    args.clip = public_id(args.clip, "clip")
    clip = project.clip(document, args.clip)
    if args.outcome not in OUTCOMES:
        die("invalid outcome")
    rel = norm_rel(args.file)
    if not rel or not project.rel_ok(rel) or not os.path.isfile(os.path.join(project.dir, rel)):
        die("delivered file missing or outside project")
    versions = clip.setdefault("versions", [])
    for version in versions:
        version["superseded"] = True
    next_version = max((int(version.get("v", 0)) for version in versions), default=0) + 1
    version_entry = {
        "v": next_version, "file": rel, "created": now(),
        "note": public_text(args.note, "note"), "superseded": False,
    }
    duration = probe_media_duration(os.path.join(project.dir, rel))
    if duration is not None:
        version_entry["duration"] = round(duration, 6)
        clip["duration"] = round(duration, 6)
    versions.append(version_entry)
    clip.update({"current": next_version, "status": "delivered", "outcome": args.outcome, "revision_note": None})
    if not args.no_derivatives:
        for key, value in derivatives(project, args.clip, rel).items():
            clip[key] = value
    remaining = [item["id"] for item in document["clips"] if item.get("status") not in ("delivered", "superseded")]
    document["status"] = "generating" if remaining else "reviewing"
    project.save(document, "clip.delivered", {"clip": args.clip, "v": next_version, "outcome": args.outcome})
    project.state(phase=document["status"], active_clip=(remaining[0] if remaining else None), queue=[])
    print(json.dumps({
        "ok": True, "clip": args.clip, "v": next_version,
        "derivatives": {key: clip.get(key) for key in ("poster", "proxy", "filmstrip")},
    }, ensure_ascii=False))


def cmd_asset(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    rel = norm_rel(args.file)
    if not rel or not project.rel_ok(rel) or not os.path.isfile(os.path.join(project.dir, rel)):
        die("asset file missing or outside project")
    if args.kind not in MEDIA_KINDS or args.origin not in MEDIA_ORIGINS:
        die("invalid asset kind or origin")
    asset_id = args.id or stable_id("as", rel)
    if not ID_RE.match(asset_id):
        die("invalid asset id")
    with open(os.path.join(project.dir, rel), "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    batch = public_text(args.batch, "batch")
    parent = public_text(args.parent, "parent")
    gen_prompt = public_text(args.gen_prompt, "gen_prompt") if args.gen_prompt else None
    entry = {
        "id": asset_id, "kind": args.kind, "file": rel,
        "name": public_text(args.name, "name") or os.path.basename(rel),
        "origin": args.origin, "group": public_text(args.group, "group"),
        "tags": [], "poster": None, "used_by": [], "status": "active",
        "hash": "sha256:" + digest,
    }
    if args.origin == "generated" and gen_prompt:
        prompt = gen_prompt
        sidecar = rel + ".json"
        jwrite(os.path.join(project.dir, sidecar), {
            "prompt": prompt, "model_label": args.kind, "params": {},
            "batch": batch, "parent": parent, "created": now(),
        })
        entry["gen"] = {
            "prompt_summary": (prompt or "")[:120], "batch": batch,
            "parent": parent, "sidecar": sidecar,
        }
    media_list = document["asset"]["media"]
    existing = next((item for item in media_list
                     if isinstance(item, dict) and item.get("id") == asset_id), None)
    if existing is not None and str(existing.get("hash") or "") != entry["hash"]:
        # A different delivery reusing an id must never erase the earlier one:
        # derive a distinct id (file + digest) so both records survive.  The same
        # bytes re-registered stay idempotent and simply refresh the entry.
        asset_id = stable_id("as", rel, digest)
        entry["id"] = asset_id
    document["asset"]["media"] = [item for item in media_list if item.get("id") != asset_id] + [entry]
    project.save(document, "asset.registered", {"asset": asset_id})
    print(json.dumps({"ok": True, "asset": asset_id}, ensure_ascii=False))


def cmd_script(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    rel = norm_rel(args.file)
    if not rel or not project.rel_ok(rel) or not os.path.isfile(os.path.join(project.dir, rel)):
        die("script file missing or outside project")
    if args.role not in SCRIPT_ROLES:
        die("invalid script role")
    script_id = public_id(args.id, "script") if args.id else stable_id("scr", rel, args.role)
    related_clips = [public_id(item, "clip") for item in (args.clip or [])]
    entry = {
        "id": script_id, "role": args.role, "file": rel,
        "name": public_text(args.name, "name") or os.path.basename(rel),
        "format": public_text(args.format, "format") or Path(rel).suffix.lstrip("."),
        "related_clips": related_clips, "status": "active", "created": now(),
    }
    document["asset"]["scripts"] = [item for item in document["asset"]["scripts"] if item.get("id") != script_id] + [entry]
    project.save(document, "script.registered", {"script": script_id})
    print(json.dumps({"ok": True, "script": script_id}, ensure_ascii=False))


def cmd_final(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    rel = norm_rel(args.file)
    if not rel or not project.rel_ok(rel) or not os.path.isfile(os.path.join(project.dir, rel)):
        die("final file missing or outside project")
    if args.kind not in FINAL_KINDS:
        die("invalid final kind")
    final_id = public_id(args.id, "final") if args.id else ("final_official" if args.kind == "official" else stable_id("final", rel, args.kind, args.preset))
    source_clips = [public_id(item, "clip") for item in (args.clip or [])]
    entry = {
        "id": final_id, "kind": args.kind, "file": rel,
        "name": public_text(args.name, "name") or os.path.basename(rel),
        "preset": public_text(args.preset, "preset"),
        "from": source_clips or list(document.get("assembly", {}).get("order", [])),
        "created": now(), "status": "active",
    }
    for old in document["asset"]["finals"]:
        if old.get("kind") == args.kind and old.get("id") != final_id and args.kind in {"official", "preview"}:
            old["status"] = "superseded"
    document["asset"]["finals"] = [item for item in document["asset"]["finals"] if item.get("id") != final_id] + [entry]
    if args.kind == "official":
        document["assembly"]["official"] = rel
        document["status"] = "done"
    elif args.kind == "preview":
        document["assembly"]["preview"] = rel
    elif args.kind == "export":
        document["assembly"].setdefault("exports", []).append({
            "file": rel, "preset": args.preset or args.name,
            "created": entry["created"], "from": "assembly",
        })
    project.save(document, "final.registered", {"final": final_id, "kind": args.kind})
    if args.kind == "official":
        project.state(phase="done", active_clip=None, queue=[])
    print(json.dumps({"ok": True, "final": final_id, "file": rel}, ensure_ascii=False))


def cmd_assemble(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    order = [public_id(item, "clip") for item in args.order.split(",") if item]
    known = {clip.get("id") for clip in document["clips"]}
    bad = [item for item in order if item not in known]
    if bad:
        die(f"unknown clips in order: {bad}")
    assembly = document.setdefault("assembly", {})
    assembly["order"] = order

    # Keep the schema-1 order projection and the schema-2 primary lane in
    # lockstep when an editor timeline is already present.  The web runtime
    # performs the same projection on timeline commits, but recorder-driven
    # assembly commands must not leave a stale ``video-main`` sequence behind.
    timeline = assembly.get("timeline")
    if isinstance(timeline, dict) and isinstance(timeline.get("tracks"), list):
        tracks = timeline["tracks"]
        main = next((track for track in tracks
                     if isinstance(track, dict) and track.get("id") == "video-main"), None)
        if main is None:
            main = {"id": "video-main", "kind": "video", "clips": []}
            tracks.insert(0, main)
        elif main.get("kind") != "video" or not isinstance(main.get("clips"), list):
            # Let the normal manifest validator report an intentionally
            # malformed editor document instead of silently changing its
            # meaning.  A missing lane, however, is safe to repair above.
            die("assembly.timeline video-main track is malformed")

        old_by_clip: dict[str, dict[str, Any]] = {}
        for item in main.get("clips", []):
            if isinstance(item, dict):
                clip_id = str(item.get("clip_id") or "")
                if clip_id and clip_id not in old_by_clip:
                    old_by_clip[clip_id] = item

        rebuilt: list[dict[str, Any]] = []
        cursor = 0.0
        for index, clip_id in enumerate(order):
            clip = next((value for value in document["clips"] if value.get("id") == clip_id), None)
            if not isinstance(clip, dict):
                continue
            old = old_by_clip.get(clip_id)
            if old is not None:
                item = copy.deepcopy(old)
            else:
                versions = clip.get("versions") if isinstance(clip.get("versions"), list) else []
                current = clip.get("current")
                version_obj = next((value for value in versions
                                    if isinstance(value, dict) and value.get("v") == current), None)
                if version_obj is None:
                    version_obj = next((value for value in reversed(versions)
                                       if isinstance(value, dict)), None)
                version = version_obj.get("v") if isinstance(version_obj, dict) else None
                source_duration = _finite_number(
                    (version_obj or {}).get("duration") if isinstance(version_obj, dict) else None,
                    _finite_number(clip.get("duration"), 1.0),
                ) or 1.0
                item = {
                    "id": stable_id("tl", "video-main", clip_id, version or "latest", index),
                    "clip_id": clip_id,
                    "version": version,
                    "start": 0.0,
                    "in": 0.0,
                    "out": source_duration,
                    "duration": source_duration,
                    "speed": 1.0,
                    "transform": {"x": 0, "y": 0, "scale": 1, "rotate": 0},
                    "transition_in": {"kind": "cut", "duration": 0},
                    "transition_out": {"kind": "cut", "duration": 0},
                }

            # Reordering is a sequence operation: retain trim/speed and all
            # presentation properties, changing only the clip's timeline start.
            duration = _finite_number(item.get("duration"), 0.0)
            if duration is None or duration <= 0:
                duration = _finite_number(clip.get("duration"), 1.0) or 1.0
                item["duration"] = duration
            item["start"] = round(cursor, 6)
            item["clip_id"] = clip_id
            rebuilt.append(item)
            cursor += duration

        main["clips"] = rebuilt
        # Ensure a timeline written by the recorder contains the stable V1
        # track contract.  Existing non-primary tracks are preserved.
        present = {str(track.get("id")) for track in tracks if isinstance(track, dict)}
        for track_id, kind in TIMELINE_TRACKS.items():
            if track_id in present:
                continue
            tracks.append({"id": track_id, "kind": kind,
                           "cues": [] if kind == "subtitle" else None,
                           "clips": [] if kind != "subtitle" else None})
        for track in tracks:
            if not isinstance(track, dict):
                continue
            if track.get("kind") == "subtitle":
                track.pop("clips", None)
                track.setdefault("cues", [])
            elif track.get("kind") in {"video", "audio"}:
                track.pop("cues", None)
                track.setdefault("clips", [])
    project.save(document, "assembly.ordered", {"order": order})
    print(json.dumps({"ok": True, "order": order}))


def cmd_official(args: argparse.Namespace) -> None:
    args.kind = "official"
    args.name = "Official final"
    args.preset = None
    args.id = "final_official"
    args.clip = None
    cmd_final(args)


def cmd_status(args: argparse.Namespace) -> None:
    project = P(args.root, args.project)
    document = project.load()
    if args.set not in PROJECT_ST:
        die("bad status")
    document["status"] = args.set
    project.save(document, "status.changed", {"to": args.set})
    project.state(phase=args.set)
    print(json.dumps({"ok": True, "status": args.set}))


def cmd_active(args: argparse.Namespace) -> None:
    os.makedirs(args.root, exist_ok=True)
    path = os.path.join(args.root, "active_project")
    if args.set:
        P(args.root, args.set).load()
        Path(path).write_text(args.set, encoding="utf-8")
    try:
        current = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    print(json.dumps({"ok": True, "active": current or None}, ensure_ascii=False))


def check_one(root: str, slug: str) -> list[str]:
    try:
        project = P(root, slug)
    except SystemExit:
        return [f"{slug}: invalid slug"]
    raw = jload(project.pj)
    if raw is None:
        return [f"{slug}: project.json missing/unreadable"]
    try:
        document = normalize_project(raw, slug)
        validate_project(document, project.dir)
    except SystemExit as exc:
        return [f"{slug}: validation failed ({exc})"]
    issues: list[str] = []
    clip_ids = [clip.get("id") for clip in document["clips"]]
    if len(clip_ids) != len(set(clip_ids)):
        issues.append("duplicate clip ids")
    for clip in document["clips"]:
        if clip.get("status") not in CLIP_ST:
            issues.append(f"{clip.get('id')}: bad status")
        if clip.get("status") == "delivered":
            versions = clip.get("versions") or []
            if not versions or clip.get("current") not in [version.get("v") for version in versions]:
                issues.append(f"{clip.get('id')}: invalid current version")
            if clip.get("outcome") not in OUTCOMES:
                issues.append(f"{clip.get('id')}: invalid outcome")
        for version in clip.get("versions", []):
            rel = norm_rel(version.get("file"))
            if not rel or not os.path.isfile(os.path.join(project.dir, rel)):
                issues.append(f"{clip.get('id')}: missing version file")
        for key in ("poster", "proxy", "filmstrip"):
            rel = clip.get(key)
            if rel and (not norm_rel(rel) or not os.path.isfile(os.path.join(project.dir, norm_rel(rel) or ""))):
                issues.append(f"{clip.get('id')}: missing {key}")
    for group in ("scripts", "media", "finals"):
        ids = [item.get("id") for item in document["asset"][group]]
        if len(ids) != len(set(ids)):
            issues.append(f"duplicate {group} ids")
        for item in document["asset"][group]:
            rel = norm_rel(item.get("file"))
            if rel and not os.path.isfile(os.path.join(project.dir, rel)):
                issues.append(f"{group} {item.get('id')}: missing file")
    known = {clip.get("id") for clip in document["clips"]}
    issues.extend(
        f"assembly references unknown clip {clip_id}"
        for clip_id in document["assembly"].get("order", [])
        if clip_id not in known
    )
    return [f"{slug}: {issue}" for issue in issues]


def cmd_check(args: argparse.Namespace) -> None:
    if not os.path.isdir(args.root):
        die(f"workspace root not found: {args.root}")
    slugs = [args.project] if args.project else [
        entry for entry in sorted(os.listdir(args.root))
        if os.path.isfile(os.path.join(args.root, entry, "project.json"))
    ]
    issues: list[str] = []
    for slug in slugs:
        issues.extend(check_one(args.root, slug))
    sync_index(args.root)
    if issues:
        print(json.dumps({"ok": False, "issues": issues}, ensure_ascii=False, indent=1))
        raise SystemExit(2)
    print(json.dumps({"ok": True, "checked": slugs, "issues": []}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=DEF_ROOT)
    sub = parser.add_subparsers(dest="cmd", required=True)
    item = sub.add_parser("create")
    item.add_argument("--title", required=True)
    item.add_argument("--type", choices=["clone", "generate"], default="clone")
    item.add_argument("--slug")
    item.add_argument("--ratio", default="9:16")
    item.set_defaults(fn=cmd_create)
    item = sub.add_parser("segments")
    item.add_argument("--project", required=True)
    item.add_argument("--file", required=True)
    item.add_argument("--source-file")
    item.add_argument("--duration", type=float)
    item.set_defaults(fn=cmd_segments)
    item = sub.add_parser("plan")
    item.add_argument("--project", required=True)
    item.add_argument("--clip", required=True)
    item.add_argument("--title")
    item.add_argument("--segment")
    item.add_argument("--duration", type=float, required=True)
    item.add_argument("--ratio", default="9:16")
    item.add_argument("--clarity", default="Standard")
    item.add_argument("--mapping", action="append")
    item.add_argument("--preserve")
    item.set_defaults(fn=cmd_plan)
    item = sub.add_parser("confirm")
    item.add_argument("--project", required=True)
    item.add_argument("--clip", action="append")
    item.set_defaults(fn=cmd_confirm)
    item = sub.add_parser("start")
    item.add_argument("--project", required=True)
    item.add_argument("--clip", required=True)
    item.set_defaults(fn=cmd_start)
    item = sub.add_parser("delivery")
    item.add_argument("--project", required=True)
    item.add_argument("--clip", required=True)
    item.add_argument("--file", required=True)
    item.add_argument("--outcome", required=True)
    item.add_argument("--note")
    item.add_argument("--no-derivatives", action="store_true")
    item.set_defaults(fn=cmd_delivery)
    item = sub.add_parser("asset")
    item.add_argument("--project", required=True)
    item.add_argument("--file", required=True)
    item.add_argument("--kind", choices=sorted(MEDIA_KINDS), required=True)
    item.add_argument("--name", required=True)
    item.add_argument("--origin", choices=sorted(MEDIA_ORIGINS), default="upload")
    item.add_argument("--gen-prompt")
    item.add_argument("--parent")
    item.add_argument("--batch")
    item.add_argument("--group")
    item.add_argument("--id")
    item.set_defaults(fn=cmd_asset)
    item = sub.add_parser("script")
    item.add_argument("--project", required=True)
    item.add_argument("--file", required=True)
    item.add_argument("--role", choices=sorted(SCRIPT_ROLES), default="script")
    item.add_argument("--name")
    item.add_argument("--format")
    item.add_argument("--clip", action="append")
    item.add_argument("--id")
    item.set_defaults(fn=cmd_script)
    item = sub.add_parser("final")
    item.add_argument("--project", required=True)
    item.add_argument("--file", required=True)
    item.add_argument("--kind", choices=sorted(FINAL_KINDS), required=True)
    item.add_argument("--name")
    item.add_argument("--preset")
    item.add_argument("--clip", action="append")
    item.add_argument("--id")
    item.set_defaults(fn=cmd_final)
    item = sub.add_parser("assemble")
    item.add_argument("--project", required=True)
    item.add_argument("--order", required=True)
    item.set_defaults(fn=cmd_assemble)
    item = sub.add_parser("official")
    item.add_argument("--project", required=True)
    item.add_argument("--file", required=True)
    item.set_defaults(fn=cmd_official)
    item = sub.add_parser("status")
    item.add_argument("--project", required=True)
    item.add_argument("--set", required=True)
    item.set_defaults(fn=cmd_status)
    item = sub.add_parser("active")
    item.add_argument("--set")
    item.set_defaults(fn=cmd_active)
    item = sub.add_parser("check")
    item.add_argument("--project")
    item.add_argument("--all", action="store_true")
    item.set_defaults(fn=cmd_check)
    args = parser.parse_args()
    args.root = os.path.abspath(os.path.expanduser(args.root))
    if args.cmd != "create" and not os.path.isdir(args.root):
        die(f"workspace root not found: {args.root}")
    args.fn(args)


if __name__ == "__main__":
    main()
