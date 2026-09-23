#!/usr/bin/env python3
"""Local resource-management server for video-asset-manager.

This service serves one management window and project files from a single
workspace. It performs only allowlisted filesystem/ffmpeg operations; it never
calls a generation provider and never publishes a website.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import base64
import json
import mimetypes
import urllib.parse
import math
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# Some Windows-distributed Python runtimes (notably the Python bundled with
# LibreOffice) ship a 3.12 ``shutil`` that expects this private ``os`` flag
# while the matching ``os`` module does not define it.  The manager uses
# ``TemporaryDirectory`` for bounded FFmpeg jobs; without the compatibility
# value a successful render is reported as failed during temporary-directory
# cleanup.  Keep the shim narrow and harmless on normal CPython builds.
if os.name == "nt" and not hasattr(os, "_walk_symlinks_as_files"):
    setattr(os, "_walk_symlinks_as_files", False)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PRIVACY_DIR = os.path.join(os.path.dirname(os.path.dirname(MODULE_DIR)), "scripts")
for _module_path in (PRIVACY_DIR, MODULE_DIR):
    if os.path.isdir(_module_path) and _module_path not in sys.path:
        sys.path.insert(0, _module_path)
try:
    from vpm_privacy import (  # type: ignore[import-not-found]
        contains_private_text as _privacy_contains_private_text,
        manager_child_environment as _privacy_manager_child_environment,
        sanitize_public_text as _privacy_sanitize_public_text,
    )
except Exception:  # pragma: no cover - old installed bundle fallback
    def _privacy_contains_private_text(value: object, *, allow_public_url: bool = False) -> bool:
        return False

    def _privacy_sanitize_public_text(value: object, *, limit: int = 4000,
                                      fallback: str | None = None,
                                      allow_public_url: bool = False) -> str | None:
        text = str(value or "").strip()
        return text[:limit] if text else fallback

    def manager_child_environment() -> dict[str, str]:
        child_env = os.environ.copy()
        for name in list(child_env):
            if re.search(
                r"(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|AUTH|CREDENTIAL|PRIVATE[_-]?KEY|SIGNATURE)",
                str(name), re.I,
            ):
                child_env.pop(name, None)
        return child_env


def default_root() -> str:
    explicit = os.environ.get("VIDEO_ASSET_MANAGER_ROOT") or os.environ.get("VPM_ROOT")
    if not explicit:
        # Capafy instances keep /home/user/workspace across resets, so prefer the
        # team's agreed projects path.  Use it when it exists, or create it when its
        # persistent parent does (a development machine has neither and therefore
        # keeps its previous default untouched).
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
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    workspace = os.environ.get("CAPAFY_WORKSPACE")
    if workspace:
        return os.path.abspath(os.path.join(os.path.expanduser(workspace), ".capafy", "video-asset-manager"))
    return os.path.abspath(os.path.join(os.path.expanduser("~"), "workspace", ".capafy", "video-asset-manager"))


def default_bind() -> str:
    """Return the bind address for the shared management runtime."""

    explicit = os.environ.get("VAM_BIND")
    if explicit and explicit.strip():
        return explicit.strip()
    return "0.0.0.0"


ROOT = default_root()
LOCK = threading.RLock()
BUNDLE_DIR = MODULE_DIR
RUNTIME_ID = "video-asset-manager"
# The manager is a standalone application rooted at `/` on its fixed port.
PREVIEW_BASE = "/"
# A manager window may stay open while a generator handoff finishes.  A tiny,
# throttled local scanner keeps the list/detail views current without
# restarting the runtime or ever calling a generation provider.
SYNC_STATE_LOCK = threading.Lock()
SYNC_RUNNING = False
SYNC_LAST_STARTED = 0.0
SYNC_MIN_INTERVAL = 3.0
SYNC_TIMEOUT = 300.0
SYNC_STATUS: dict[str, object] = {
    "state": "starting",
    "last_scan": None,
    "scanned": 0,
    "received": 0,
    "pending": 0,
    "unmatched": 0,
    "delivery_failed": 0,
    "failed": 0,
}

# ``index.json`` is a projection maintained by the recorder/scanner.  Reading
# it is cheap; rebuilding it is not (``sync_index`` walks every public project
# file to calculate portable sizes).  Keep a process-local snapshot and probe
# only direct project manifests at a bounded cadence.  This keeps the common
# browser GET path out of the recursive index rebuild while still noticing an
# external project create or manifest edit.
INDEX_CACHE_LOCK = threading.RLock()
INDEX_CACHE: dict[str, object] | None = None
INDEX_CACHE_FILE_SIG: tuple[int, int] | None = None
INDEX_CACHE_PROJECT_SIG: tuple[tuple[str, int, int], ...] | None = None
INDEX_CACHE_ROOT: str | None = None
INDEX_CACHE_LAST_PROBE = 0.0
# A direct-child manifest probe is intentionally cheap enough for every list
# request.  Keep this as a named knob for embedded hosts that choose to add a
# small debounce, but default to zero so a newly delivered project appears on
# the next API response.
INDEX_PROBE_INTERVAL = 0.0


def _pending_row_counts(rows: object) -> tuple[int, int]:
    """Return ``(unmatched, delivery_failed)`` for scanner pending rows."""

    if not isinstance(rows, list):
        return 0, 0
    markers = (
        "当前项目未选择", "当前项目不存在", "当前项目不可用", "目标项目不存在",
        "指定的项目", "任务映射", "项目映射", "项目 slug", "项目无效",
    )
    unmatched = delivery = 0
    for item in rows[:10000]:
        if not isinstance(item, dict):
            continue
        if item.get("ok") is True:
            continue
        category = str(item.get("category") or "").strip().lower()
        reason = str(item.get("reason") or "").strip().lower()
        is_unmatched = category in {"unmatched", "unmatched_project", "project_mapping"}
        if not is_unmatched:
            is_unmatched = any(marker.lower() in reason for marker in markers)
        if is_unmatched:
            unmatched += 1
        else:
            delivery += 1
    return min(unmatched, 10000), min(delivery, 10000)


# Keep the old name as the active interval for compatibility with installed
# launchers/tests.  The watcher backs off to the idle/error intervals when no
# handoff directory entries changed.
WATCH_INTERVAL = 3.0
WATCH_IDLE_INTERVAL = 20.0
WATCH_ERROR_INTERVAL = 10.0
WATCH_SIGNATURE_LIMIT = 2000
WATCH_RETRY_INTERVAL = 30.0
WATCHER_STOP = threading.Event()
WATCHER_THREAD: threading.Thread | None = None
TASK_DIR: str | None = None
TASK_MAP: str | None = None
# The HTTP manager and its scanner do not need provider/storage credentials.
# Strip known generation credentials from scanner children while preserving
# ordinary workspace and locale settings.
PRIVATE_CHILD_ENV_NAMES = (
    "OPENAI_API_KEY", "ARK_API_KEY",
    "TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_SESSION_TOKEN",
    "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "VIDEO_GENERATOR_API_KEY",
)
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# Upload policy shared with the browser: our own code (not a skill contract).
# Deliberately rejected: SVG can carry active content, and HEIC/HEIF is decoded
# unreliably by the bundled Windows ffmpeg, which would produce uploads with no
# thumbnail, poster or usable preview.
UPLOAD_LIMITS = {"video": 200 * 1024 * 1024, "image": 20 * 1024 * 1024, "audio": 50 * 1024 * 1024}

# Delivery spec: MP4/H.264 VBR with a target bitrate chosen by resolution tier and
# frame-rate band, scaled by the quality option.  The quality switch only changes
# the target bitrate -- never the size, frame rate, speed or duration.
EXPORT_BITRATE_TIERS = ((480, 2500, 4000), (720, 5000, 7500), (1080, 8000, 12000))
EXPORT_QUALITY_FACTORS = {"recommended": 1.0, "high": 1.5, "smaller": 0.6}
# Audio for every delivered file: AAC, 48 kHz, 192 kbps, stereo.
EXPORT_AUDIO_RATE = 48000
EXPORT_AUDIO_BITRATE = "192k"


def normalize_quality(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"high", "high quality", "18"}:
        return "high"
    if text in {"smaller", "smaller file", "28"}:
        return "smaller"
    return "recommended"


def canvas_for_video(width: object, height: object) -> dict:
    """Canvas settings for the first generated video of a project.

    The spec adopts the first successful generation's own pixel size (and frame
    rate) as the project parameters, and only the nearest supported ratio is
    recorded for the UI.
    """

    try:
        w = int(width or 0)
        h = int(height or 0)
    except (TypeError, ValueError, OverflowError):
        return {}
    if w <= 0 or h <= 0:
        return {}
    if h >= w * 1.6:
        ratio = "9:16"
    elif w >= h * 1.6:
        ratio = "16:9"
    elif h > w:
        ratio = "4:5"
    else:
        ratio = "1:1"
    return {"ratio": ratio, "width": w, "height": h}


def timeline_clip_count(document: dict) -> int:
    """Number of timeline clips on every video track of a normalized project."""

    timeline = ((document.get("assembly") or {}).get("timeline") or {})
    total = 0
    for track in timeline.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        if str(track.get("kind") or "") == "video":
            total += len([item for item in (track.get("clips") or []) if isinstance(item, dict)])
    return total

def first_video_plan(document: dict, width: object, height: object, fps: object) -> dict:
    """What a newly received generated video does to its project.

    Spec: the first successful generation of an instance starts the project with
    the video's own pixel size and frame rate and places it at the start of the
    timeline; every later success only joins the media library.
    """

    if timeline_clip_count(document) > 0:
        return {"adopt": False}
    canvas = canvas_for_video(width, height)
    plan: dict = {"adopt": True, "start": 0.0}
    if canvas:
        plan["canvas"] = canvas
    try:
        rate = int(fps or 0)
    except (TypeError, ValueError, OverflowError):
        rate = 0
    if rate > 0:
        plan["fps"] = rate
    return plan

def apply_first_video_adoption(slug: str, document: dict, asset_id: str, rel: str,
                               width: int, height: int, fps: int, duration: float,
                               name: str | None = None) -> str | None:
    """Give a project its opening clip from its first generated video.

    A video track only accepts clip references, so this creates a real clip
    record (``clips/<clip_id>/v1.mp4`` plus a manifest entry) and references it
    from the timeline.  Writing a media-only reference instead produces a
    timeline that every later save rejects, so this fails closed: nothing is
    written unless the whole record can be produced.
    """

    import hashlib

    plan = first_video_plan(document, width, height, fps)
    if not plan.get("adopt"):
        return None
    source = safe_file(slug, rel or "")
    if not source:
        return None
    digest = hashlib.sha1(str(asset_id or rel).encode("utf-8")).hexdigest()[:12]
    clip_id = f"clip_media_{digest}"
    dest_rel = f"clips/{clip_id}/v1.mp4"
    dest = os.path.join(project_dir(slug), *dest_rel.split("/"))
    if not safe_write_target(project_dir(slug), dest):
        return None

    records = document.setdefault("clips", [])
    if not isinstance(records, list):
        return None
    record = next((item for item in records if isinstance(item, dict) and item.get("id") == clip_id), None)
    stamp = now()
    ratio = (plan.get("canvas") or {}).get("ratio") or "9:16"
    if record is None:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not os.path.isfile(dest):
                shutil.copyfile(source, dest)
        except OSError:
            return None
        record = {
            "id": clip_id,
            "title": str(name or os.path.basename(rel or "clip"))[:160],
            "duration": duration,
            "ratio": ratio,
            "clarity": str((document.get("presets") or {}).get("clarity") or "Standard"),
            "status": "delivered",
            "outcome": "complete",
            "current": 1,
            "origin": "generated",
            "media_id": asset_id,
            "mappings": [asset_id],
            "created": stamp,
            "poster": None,
            "versions": [{"v": 1, "file": dest_rel, "duration": duration, "created": stamp,
                          "note": "Prepared from project media", "superseded": False}],
        }
        records.append(record)

    assembly = document.setdefault("assembly", {})
    timeline = assembly.get("timeline")
    if not isinstance(timeline, dict):
        timeline = {"canvas": dict(document.get("presets") or {}), "tracks": [
            {"id": "text-main", "kind": "subtitle", "muted": False, "hidden": False, "clips": [], "cues": []},
            {"id": "video-main", "kind": "video", "muted": False, "hidden": False, "clips": []},
            {"id": "video-overlay", "kind": "video", "muted": False, "hidden": False, "clips": []},
            {"id": "video-2", "kind": "video", "muted": False, "hidden": False, "clips": []},
            {"id": "video-3", "kind": "video", "muted": False, "hidden": False, "clips": []},
            {"id": "audio-main", "kind": "audio", "muted": False, "hidden": False, "clips": []},
        ]}
        assembly["timeline"] = timeline
    track = next((item for item in (timeline.get("tracks") or [])
                  if isinstance(item, dict) and item.get("id") == "video-main"), None)
    if track is None:
        return None
    if plan.get("canvas"):
        timeline["canvas"] = {**(timeline.get("canvas") or {}), **plan["canvas"]}
        document.setdefault("presets", {})["ratio"] = plan["canvas"]["ratio"]
    if plan.get("fps"):
        timeline["fps"] = plan["fps"]
    item = {
        "id": stable_id("tl", "video-main", clip_id, 1, 0, 0.0, 0.0, duration),
        "clip_id": clip_id,
        "media_id": None,
        "version": 1,
        "source_duration": duration,
        "start": 0.0,
        "in": 0.0,
        "out": duration,
        "duration": duration,
        "speed": 1.0,
        "transform": {"x": 0.0, "y": 0.0, "scale": 1.0, "rotate": 0.0, "opacity": 1.0, "border": 0.0},
        "link_group": None,
        "transition_in": {"kind": "cut", "duration": 0},
        "transition_out": {"kind": "cut", "duration": 0},
    }
    track.setdefault("clips", []).append(item)
    document["rev"] = max(0, int(document.get("rev", 0) or 0)) + 1
    document["updated"] = stamp
    return item["id"]


def adopt_pending_first_video(limit: int = 6) -> int:
    """Give projects that have media but no timeline clip their opening video.

    Runs after a generator scan: the first successful generation of an instance
    starts the project with its own pixel size and frame rate; later generations
    only join the library, which is why this stops at the first adopted clip.
    """

    adopted = 0
    try:
        index = read_index_cached()
    except (OSError, ValueError, TypeError):
        return 0
    entries = index.get("projects") if isinstance(index, dict) else None
    for entry in (entries or [])[:max(1, limit)]:
        slug = str((entry or {}).get("slug") or "").strip() if isinstance(entry, dict) else ""
        if not slug or not SLUG_RE.match(slug) or is_reserved_slug(slug):
            continue
        try:
            with LOCK:
                raw = read_json(project_file(slug), None)
                if not isinstance(raw, dict):
                    continue
                document = normalize_project(raw, slug)
                if timeline_clip_count(document) > 0:
                    continue
                media = [item for item in ((document.get("asset") or {}).get("media") or [])
                         if isinstance(item, dict) and str(item.get("kind") or "") == "video"]
                media.sort(key=lambda item: str(item.get("created") or ""))
                source = next((item for item in reversed(media)
                               if safe_file(slug, str(item.get("file") or ""))), None)
                if source is None:
                    continue
                info = _probe_media_info(safe_file(slug, str(source.get("file") or "")))
                width = int(info.get("width") or 0)
                height = int(info.get("height") or 0)
                fps = int(info.get("fps") or 0)
                duration = max(0.1, min(24 * 60 * 60, _number(info.get("duration"), 1.0)))
                clip_id = apply_first_video_adoption(
                    slug, document, str(source.get("id") or ""), str(source.get("file") or ""),
                    width, height, fps, duration)
                if not clip_id:
                    continue
                write_json_atomic(project_file(slug), document)
                append_ops(slug, [{"op": "timeline.adopt_first_video", "item_id": clip_id,
                                   "asset_id": str(source.get("id") or "")}], document["rev"])
                adopted += 1
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return adopted


def export_bitrate_kbps(width: int, height: int, fps: float, quality: object = "recommended") -> int:
    """Target VBR bitrate in kbps for a delivered render."""

    short = min(int(width or 0), int(height or 0))
    if short <= 0:
        short = 1080
    tier = 480 if short <= 540 else 720 if short <= 900 else 1080
    for limit, low, high in EXPORT_BITRATE_TIERS:
        if tier == limit:
            base = high if float(fps or 0) > 30 else low
            break
    else:  # pragma: no cover - tier is always one of the table rows
        base = 8000
    factor = EXPORT_QUALITY_FACTORS[normalize_quality(quality)]
    return max(200, int(round(base * factor)))
UPLOAD_REJECT_EXT = {".svg", ".heic", ".heif"}
UPLOAD_MAX_BODY = UPLOAD_LIMITS["video"] + 16 * 1024 * 1024
UPLOAD_HELP = ("Supported: MP4, M4V, MOV, WEBM, MKV, AVI, MPG, MPEG, TS, M2TS, OGV, 3GP, FLV, "
               "JPG, PNG, WEBP, GIF, BMP, MP3, WAV, M4A, AAC, OGG, OPUS, FLAC. "
               "SVG, HEIC and documents are not supported.")
VID_EXT = {
    ".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi", ".mpeg", ".mpg",
    ".ts", ".ogv", ".3gp", ".flv",
}
AUD_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,79}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$")
PRIVATE_RE = re.compile(
    r"(?:api[_-]?key|apikey|access[_-]?key|secret(?:[_-]?key)?|password|passwd|"
    r"authorization|bearer\s|(?:access|refresh|session)[_-]?token|"
    r"(?<![a-z])token(?![a-z])|credential|private[_-]?key|signature|signed[_-]?url|"
    r"provider|model[_-]?route|stack\s*trace|x-amz-|s3://)",
    re.I,
)
ABSOLUTE_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|users|tmp|var|private|root|workspace|mnt|opt|"
    r"srv|etc|run|proc|sys|program\s+files|windows)(?:[\\/]|$))",
    re.I,
)
# URLs are transport references, not project-relative resources.  They may
# contain credentials in query/fragment components (for example signed S3
# URLs), so never expose them through the manager API or static file surface.
REMOTE_REFERENCE_RE = re.compile(
    r"(?:\b(?:https?|s3|ftp|file|data)://|(?<!\w)//[^\s]+)", re.I
)
QUERY_SECRET_RE = re.compile(
    r"[?&](?:api[_-]?key|access[_-]?key|secret|token|signature|sig|auth|"
    r"credential|x-amz-[^=&#\s]+)\s*=", re.I
)
RESERVED = {"webapp", "trash", "api", "staging", "inbox", "tasks"}

# Personal data and credential shapes that are not covered by the key/value
# patterns above: an e-mail address, a provider-style API key, a long
# hex/base64 secret, or a JWT.  The operation log is customer-visible and is
# also fed to the model context, so none of these may ever reach it.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}", re.I)
CREDENTIAL_RE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}|\beyJ[A-Za-z0-9_-]{10,}|\bxox[baprs]-[A-Za-z0-9-]{8,}|"
    r"\b(?:ghp|gho|glpat)_[A-Za-z0-9_-]{10,}|\b[A-Za-z0-9+/]{60,}={0,2}\b)"
)
LOG_VALUE_MAX = 200
LOG_KEYS_MAX = 24


# Keys that never belong in the log whatever their value looks like: customer
# creative text, request bodies, credentials and transport references.
LOG_DROP_KEYS = {
    "prompt", "body", "token", "url", "text", "note", "message", "description",
    "content", "caption", "html", "raw", "headers", "authorization", "cookie",
}


def _log_value_safe(key: object, value: object) -> object:
    """Return a value that is safe to store in the customer-visible log."""

    text = str(key)
    if text.strip().lower() in LOG_DROP_KEYS:
        return None
    if (PRIVATE_RE.search(text) or ABSOLUTE_RE.search(text)
            or EMAIL_RE.search(text) or CREDENTIAL_RE.search(text)):
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if (PRIVATE_RE.search(value) or ABSOLUTE_RE.search(value)
                or REMOTE_REFERENCE_RE.search(value) or QUERY_SECRET_RE.search(value)
                or EMAIL_RE.search(value) or CREDENTIAL_RE.search(value)):
            return None
        stripped = value.replace("\x00", "").strip()
        if not stripped:
            return None
        return stripped[:LOG_VALUE_MAX]
    if isinstance(value, list):
        items = []
        for item in value[:LOG_KEYS_MAX]:
            if isinstance(item, (str, int, float, bool)) or item is None:
                cleaned = _log_value_safe("", item)
                if cleaned is not None:
                    items.append(cleaned)
        return items
    # Objects (for example a whole cue or a render payload) are never logged.
    return None


def sanitize_log_entry(raw: object) -> dict:
    """Normalize one operation-log record for storage or for the read API.

    Applied on write *and* on read: older log lines predate the current rules,
    so the read path must not trust the file either.
    """

    if not isinstance(raw, dict):
        return {}
    clean: dict = {}
    for key, value in raw.items():
        name = str(key)
        if name in {"op", "ts"}:
            value_ok = _log_value_safe("", value)
            if value_ok is not None:
                clean[name] = value_ok
            continue
        cleaned = _log_value_safe(name, value)
        if cleaned is not None and cleaned != [] :
            clean[name] = cleaned
        if len(clean) >= LOG_KEYS_MAX:
            break
    return clean
PRIVATE_STATIC_SEGMENTS = {"inbox", "staging", "trash", "tasks", "__pycache__"}
PRIVATE_STATIC_FILENAMES = {
    ".handoff_pending.json", "active_project", "index.json", "project.json",
    "state.json", "runtime.json", "server.log",
}
PUBLIC_PROJECT_DIRS = {"assets", "source", "clips", "output", "media", "subtitles", "logs"}
PUBLIC_ASSET_DIRS = {"uploads", "generated", "scripts", "reports"}
PUBLIC_LOG_FILES = {"edits.log", "timeline.jsonl"}
PUBLIC_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".jsonl", ".srt", ".vtt", ".ass"}
PUBLIC_WEBAPP_FILES = {"index.html", "workspace.css", "studio-skin.css", "inter.ttf", "roboto.ttf", "playfairdisplay.ttf", "bebasneue.ttf", "dancingscript.ttf", "workspace.js", "studio-workspace.js", "studio-panels.js", "studio-filmstrip.js"}
MEDIA_WORK_LOCK = threading.Lock()
MEDIA_MAX_BYTES = 800 * 1024 * 1024
SAVE_TOP_LEVEL_KEYS = {
    "schema", "rev", "slug", "title", "type", "status", "created", "updated",
    "presets", "source", "asset", "assets", "clips", "assembly",
}

# Studio/timeline limits.  These are deliberately conservative so a browser
# payload cannot turn the long-lived manager process into an unbounded render
# or manifest writer.  The schema remains extensible, while V1 exposes only
# the tracks and operations that the lightweight editor can render safely.
TIMELINE_TRACKS = {
    "video-main": "video",
    "video-overlay": "video",
    "video-2": "video",
    "video-3": "video",
    "audio-main": "audio",
    "text-main": "subtitle",
}
TIMELINE_MAX_TRACKS = 6
TIMELINE_MAX_ITEMS = 500
TIMELINE_MAX_CUES = 2000
TIMELINE_MAX_OPS = 100
TIMELINE_MAX_HISTORY = 20
TIMELINE_TRANSITIONS = {"cut", "fade", "dip_black", "dip_white"}
# Easing curves accepted for a caption's fade-in/out (the
# Animation tab).  Values are the CSS timing-function names used by the live
# overlay; the ASS writer maps them onto animated alpha so preview and export
# agree.
CAPTION_EASINGS = {"linear", "ease-in", "ease-out", "ease-in-out"}
# Caption animation kinds.  "fade" is the default (and the only kind the
# editor implements), so it is never persisted explicitly.
CAPTION_MOTIONS = ("none", "fade", "slide-up", "slide-down", "slide-left", "slide-right", "pop")
# Optional easing curve for the ``fade`` (cross-dissolve) transition.
# ``linear`` is the default and renders through ffmpeg's built-in xfade fade,
# so projects that never set an easing keep byte-identical exports.
#
# xfade's progress P runs 1 -> 0 across the transition window (verified
# against vf_xfade.c); the fragments below are written in that space, with
# Q = 1-P being the natural 0 -> 1 progress the browser monitor uses.
# Custom transitions evaluate per pixel per plane, so expressions MUST stay
# affine combinations A*(1-W)+B*W with W in [0,1] — anything else corrupts
# chroma.  dip_black always renders through the built-in fadeblack (which has
# its own smoothstep curve) because no custom expression can reference the
# per-plane black level it needs.
TIMELINE_EASINGS = {
    "linear": "1-P",
    "ease_in": "(1-P)*(1-P)",
    "ease_out": "1-(P*P)",
    "ease_in_out": "if(lt(1-P,0.5),2*(1-P)*(1-P),1-2*(P*P))",
}
TIMELINE_RATIOS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}
TIMELINE_FPS = {24, 25, 30, 50, 60}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Render jobs are intentionally process-local.  They are public status
# projections only; absolute paths, commands and diagnostics never leave the
# worker.  Keeping a small bounded map avoids a long-running manager growing
# without limit when a user previews repeatedly.
RENDER_LOCK = threading.RLock()
RENDER_JOBS: dict[str, dict[str, object]] = {}
RENDER_MAX_JOBS = 128
TIMELINE_HISTORY: dict[str, list[dict]] = {}
TIMELINE_REDO: dict[str, list[dict]] = {}
TIMELINE_HISTORY_HEAD: dict[str, int] = {}
TIMELINE_HISTORY_MAX_PROJECTS = 256


def _timeline_history_key(slug: str) -> str:
    """Use the project boundary, rather than only the slug, as the key.

    Tests and embedded hosts can reuse a slug after changing ``ROOT``.  A
    slug-only key would then replay snapshots from an unrelated project.
    """

    try:
        return os.path.normcase(os.path.abspath(project_file(slug)))
    except (OSError, TypeError, ValueError):
        return f"{os.path.normcase(os.path.abspath(ROOT))}|{slug}"


def _timeline_history_stacks(slug: str, revision: int) -> tuple[str, list[dict], list[dict]]:
    """Return compatible process-local history stacks for a project.

    ``project.rev`` is the invalidation fence.  Any writer (including a
    non-timeline save) increments it, so a revision mismatch clears the local
    editor stacks before they can be replayed.  The head revision is tracked
    separately from individual snapshots: undoing once increments the project
    revision but must not make older snapshots unusable for another undo.
    """

    key = _timeline_history_key(slug)
    history = TIMELINE_HISTORY.setdefault(key, [])
    redo = TIMELINE_REDO.setdefault(key, [])
    head = TIMELINE_HISTORY_HEAD.get(key)
    if head is not None and head != revision:
        history.clear()
        redo.clear()
    TIMELINE_HISTORY_HEAD[key] = revision
    # Keep process-local bookkeeping bounded even when a long-lived manager
    # visits many one-off projects.
    if len(TIMELINE_HISTORY_HEAD) > TIMELINE_HISTORY_MAX_PROJECTS:
        stale_keys = list(TIMELINE_HISTORY_HEAD)[:-TIMELINE_HISTORY_MAX_PROJECTS]
        for stale in stale_keys:
            TIMELINE_HISTORY_HEAD.pop(stale, None)
            TIMELINE_HISTORY.pop(stale, None)
            TIMELINE_REDO.pop(stale, None)
    return key, history, redo


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_reserved_slug(value: object) -> bool:
    return str(value or "").strip().casefold() in {item.casefold() for item in RESERVED}


def sync_status_snapshot() -> dict[str, object]:
    """Return aggregate handoff state safe for the management page."""

    with SYNC_STATE_LOCK:
        snapshot = dict(SYNC_STATUS)
        snapshot["running"] = bool(SYNC_RUNNING)
    counts = pending_handoff_counts()
    # ``pending`` is a manager queue count.  It is deliberately kept separate
    # from ``failed``: an upstream task may report ``status=failed`` while the
    # manager is only waiting for a project mapping, and that must never look
    # like a new video-generation failure in the UI.
    snapshot["pending"] = counts["pending"]
    snapshot["unmatched"] = counts["unmatched"]
    snapshot["delivery_failed"] = counts["delivery_failed"]
    snapshot["pending_count"] = counts["pending"]
    snapshot["unmatched_count"] = counts["unmatched"]
    snapshot["delivery_failed_count"] = counts["delivery_failed"]
    try:
        scan_failed = max(0, min(10000, int(snapshot.get("failed") or 0)))
    except (TypeError, ValueError):
        scan_failed = 0
    snapshot["failed"] = scan_failed
    snapshot["scan_failed"] = scan_failed
    return snapshot


def _update_sync_status(**updates: object) -> None:
    with SYNC_STATE_LOCK:
        SYNC_STATUS.update(updates)


def public_label(value: object, fallback: str) -> str:
    text = str(value or "").strip()[:80]
    if not text or _looks_private(text):
        return fallback
    return text


_DROP = object()


def sanitize_public(value: object, key: str = "", *, _checks: dict[str, bool] | None = None) -> object:
    """Remove private/absolute-path values before returning customer data."""

    # Field names and legacy aliases repeat throughout a response. Reuse only
    # the text classification during this call, never a mutable public payload
    # or a cross-request decision that could hide newly changed data.
    if _checks is None:
        _checks = {}

    def private(text: str) -> bool:
        if text not in _checks:
            decision = _looks_private(text)
            if len(text) > 512 or len(_checks) >= 4096:
                return decision
            _checks[text] = decision
        return _checks[text]

    if isinstance(value, dict):
        clean: dict[str, object] = {}
        for raw_key, child in value.items():
            child_key = str(raw_key)
            # Change tokens are intentionally public cache validators.  Keep
            # only the strict, locally-generated hexadecimal shape; arbitrary
            # values under a ``token``-like key remain blocked by the normal
            # privacy policy below.
            if (child_key.casefold() in {"token", "index_token"}
                    and isinstance(child, str)
                    and re.fullmatch(r"[0-9a-f]{16,64}", child.strip(), re.I)):
                clean[child_key] = child.strip().lower()
                continue
            if private(child_key):
                continue
            sanitized = sanitize_public(child, child_key, _checks=_checks)
            if sanitized is not _DROP:
                clean[child_key] = sanitized
        return clean
    if isinstance(value, list):
        clean_list = []
        for child in value:
            sanitized = sanitize_public(child, key, _checks=_checks)
            if sanitized is not _DROP:
                clean_list.append(sanitized)
        return clean_list
    if isinstance(value, str):
        if private(value):
            return _DROP
        return value
    return value


def _looks_private(value: object) -> bool:
    """Return whether a value is unsafe for a customer-visible response.

    Keep a local conservative fallback for older copied bundles while using
    the shared privacy policy when it is available.  In particular, this
    rejects signed/remote URLs and absolute paths, not just obvious key names.
    """

    text = str(value or "")
    if not text:
        return False
    candidates = [text]
    # JSON fields may contain a percent-encoded URL/path.  Check one decoded
    # view as well; this is bounded and only broadens rejection decisions.
    try:
        decoded = unquote(text)
        if decoded != text:
            candidates.append(decoded)
    except (TypeError, ValueError):
        pass
    if any(PRIVATE_RE.search(candidate) or ABSOLUTE_RE.search(candidate)
           or REMOTE_REFERENCE_RE.search(candidate)
           or QUERY_SECRET_RE.search(candidate) for candidate in candidates):
        return True
    try:
        return any(bool(_privacy_contains_private_text(candidate)) for candidate in candidates)
    except Exception:
        return False


def canonical_request_path(value: object) -> str:
    """Decode and normalize a URL path for security checks only.

    ``SimpleHTTPRequestHandler`` decodes paths internally.  Decode a bounded
    number of times here as well so double-encoded ``/inbox`` or traversal
    segments cannot bypass the private-path guard.  The original request path
    is still passed to the stdlib handler for normal filename semantics.
    """

    try:
        path = str(value or "/").split("?", 1)[0].split("#", 1)[0]
    except Exception:
        return "/"
    for _ in range(3):
        try:
            decoded = unquote(path)
        except (TypeError, ValueError):
            return "/"
        if decoded == path:
            break
        path = decoded
    return path.replace("\\", "/") or "/"


def normalize_preview_base(value: object) -> str:
    """Keep the legacy argument harmless; the manager always serves at `/`."""

    text = str(value or "").strip().replace("\\", "/")
    if text not in {"", "/"}:
        raise ValueError("manager base must be /")
    return "/"


def application_request_path(value: object) -> tuple[bool, str, str]:
    """Return ``(matched, app_path, query)`` for a raw HTTP request path.

    The manager exposes one application path rooted at `/`.
    """

    try:
        raw = str(value or "/")
    except Exception:
        return False, "/", ""
    parsed = urlparse(raw)
    path = canonical_request_path(parsed.path)
    query = parsed.query or ""
    return True, path, query


def path_contains_link(base: str, candidate: str) -> bool:
    """Return whether *candidate* traverses a symlink/junction below *base*."""

    try:
        base_abs = os.path.abspath(base)
        candidate_abs = os.path.abspath(candidate)
        if os.path.normcase(os.path.commonpath((base_abs, candidate_abs))) != os.path.normcase(base_abs):
            return True
        relative = os.path.relpath(candidate_abs, base_abs)
        cursor = base_abs
        if relative not in ("", "."):
            for part in relative.split(os.sep):
                if not part or part == ".":
                    continue
                cursor = os.path.join(cursor, part)
                if not os.path.lexists(cursor):
                    # Missing descendants cannot introduce a link yet; the
                    # existing parents have already been checked.
                    continue
                if os.path.islink(cursor):
                    return True
                # Windows junctions and mount-like reparse points may not be
                # reported by ``islink`` on every Python version.  A changed
                # realpath is a conservative signal to reject them too.
                if os.path.normcase(os.path.realpath(cursor)) != os.path.normcase(os.path.abspath(cursor)):
                    return True
        return False
    except (OSError, RuntimeError, ValueError):
        return True


def stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def norm_rel(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().replace("\\", "/")
    if "\x00" in value:
        return None
    if not value or value.startswith(("/", "//", "data:", "http:", "https:")) or re.match(r"^[A-Za-z]:", value):
        return None
    parts = [part for part in value.split("/") if part not in ("", ".")]
    # Reject alternate-data-stream syntax and Windows device names in
    # addition to the usual traversal/drive-letter checks.  These values are
    # persisted in manifests and later passed to file operations.
    devices = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if not parts or any(part == ".." or ":" in part or part.upper().split(".", 1)[0] in devices for part in parts):
        return None
    return "/".join(parts)


def path_within(base: str, candidate: str) -> bool:
    """Return whether *candidate* resolves below *base* (cross-platform)."""

    try:
        base_real = os.path.normcase(os.path.realpath(base))
        candidate_real = os.path.normcase(os.path.realpath(candidate))
        return os.path.commonpath((base_real, candidate_real)) == base_real
    except (OSError, ValueError):
        # ValueError covers drives/UNC roots that cannot share a common path.
        return False


def safe_write_target(base: str, candidate: str) -> bool:
    """Check a write target and all existing parents stay inside *base*.

    ``path_within`` alone is not enough before ``os.makedirs``: a missing
    child below a symlinked parent resolves outside only after the parent is
    followed.  Walk the existing path components first and reject links or
    non-directory parents so derived media cannot escape the project.
    """

    try:
        base_abs = os.path.abspath(base)
        target_abs = os.path.abspath(candidate)
        if not path_within(base_abs, target_abs):
            return False
        if os.path.lexists(target_abs) and (os.path.islink(target_abs) or os.path.isdir(target_abs)):
            return False
        cursor = os.path.dirname(target_abs)
        while cursor and cursor != os.path.dirname(cursor):
            if os.path.lexists(cursor):
                if os.path.islink(cursor) or not os.path.isdir(cursor):
                    return False
            if os.path.normcase(cursor) == os.path.normcase(base_abs):
                return True
            cursor = os.path.dirname(cursor)
        return os.path.normcase(cursor) == os.path.normcase(base_abs)
    except (OSError, RuntimeError, ValueError):
        return False


def read_json(path: str, default: object = None) -> object:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def pending_handoff_count() -> int:
    """Return only the number of queued handoffs, never their identifiers."""

    path = os.path.abspath(os.path.join(ROOT, ".handoff_pending.json"))
    if os.path.islink(path) or not path_within(ROOT, path):
        return 0
    value = read_json(path, {})
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return 0
    # A count is useful UI state; cap it so a malformed file cannot create a
    # surprisingly large response or message.
    return min(len(value["items"]), 10000)


def pending_handoff_summary() -> tuple[int, int]:
    """Return the legacy ``(total, failed-status)`` tuple.

    Keep this helper for older callers, but do not use its second value as a
    generation-failure counter.  A task's upstream status is not the same as a
    manager delivery failure.
    """

    path = os.path.abspath(os.path.join(ROOT, ".handoff_pending.json"))
    if os.path.islink(path) or not path_within(ROOT, path):
        return 0, 0
    value = read_json(path, {})
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list):
        return 0, 0
    total = min(len(items), 10000)
    failure_statuses = {"failed", "error", "cancelled", "canceled", "timed_out", "timeout"}
    failures = sum(
        1 for item in items[:10000]
        if isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() in failure_statuses
    )
    return total, min(failures, total)


def pending_handoff_counts() -> dict[str, int]:
    """Return queue counts split by mapping and delivery state.

    The queue file predates the explicit ``category`` field, so old entries
    are classified from their sanitized reason.  This function never returns
    task IDs, paths, or provider details.
    """

    path = os.path.abspath(os.path.join(ROOT, ".handoff_pending.json"))
    if os.path.islink(path) or not path_within(ROOT, path):
        return {"pending": 0, "unmatched": 0, "delivery_failed": 0}
    value = read_json(path, {})
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list):
        return {"pending": 0, "unmatched": 0, "delivery_failed": 0}
    unmatched_markers = (
        "当前项目未选择", "当前项目不存在", "当前项目不可用", "目标项目不存在",
        "指定的项目", "任务映射", "项目映射", "项目 slug", "项目无效",
    )
    total = unmatched = delivery = 0
    for item in items[:10000]:
        if not isinstance(item, dict):
            continue
        total += 1
        category = str(item.get("category") or "").strip().lower()
        reason = str(item.get("reason") or "").strip().lower()
        is_unmatched = category in {"unmatched", "unmatched_project", "project_mapping"}
        if not is_unmatched:
            is_unmatched = any(marker.lower() in reason for marker in unmatched_markers)
        if is_unmatched:
            unmatched += 1
        else:
            delivery += 1
    return {
        "pending": min(total, 10000),
        "unmatched": min(unmatched, 10000),
        "delivery_failed": min(delivery, 10000),
    }


def write_json_atomic(path: str, value: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        # Windows can hold a transient handle on the target (indexer, scanner); a
        # single PermissionError used to abort the request with no response at all.
        for attempt in range(6):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                time.sleep(0.06 * (attempt + 1))
        else:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=1)
                handle.write("\n")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_text_atomic(path: str, value: str) -> None:
    """Atomically replace a small text marker such as ``active_project``."""

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp.", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
        # Windows can hold a transient handle on the target (indexer, scanner); a
        # single PermissionError used to abort the request with no response at all.
        for attempt in range(6):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                time.sleep(0.06 * (attempt + 1))
        else:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=1)
                handle.write("\n")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def empty_project(slug: str = "", title: str = "", ptype: str = "clone") -> dict:
    return {
        "schema": 2, "rev": 0, "slug": slug, "title": title, "type": ptype,
        "status": "draft", "created": now(), "updated": now(),
        "presets": {"ratio": "16:9", "clarity": "Standard"},
        "source": {"file": None, "origin_url": None, "duration": None, "segments": [], "analysis": None},
        "asset": {"scripts": [], "media": [], "clips": [], "finals": []},
        "assets": [], "clips": [],
        "assembly": {"order": [], "transition": "cut",
                     "audio": {"bgm": None, "bgm_gain": 0, "mute_original": False},
                     "subtitles": {"file": None, "burn": False}, "preview": None,
                     "official": None, "exports": []},
    }


def dedupe(entries: object, prefix: str = "item") -> list[dict]:
    output: list[dict] = []
    seen: set[str] = set()
    seen_files: set[str] = set()
    if not isinstance(entries, list):
        return output
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
            output.append(copied)
    return output


def _number(value: object, default: float = 0.0) -> float:
    """Return a finite bounded float for public timeline values."""

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _atempo_chain(speed: float) -> str:
    """Build an FFmpeg atempo chain for the editor's 0.25x–4x range."""

    remaining = max(0.25, min(4.0, _number(speed, 1.0)))
    filters: list[str] = []
    while remaining < 0.5 - 1e-9:
        filters.append("atempo=0.5")
        remaining /= 0.5
    while remaining > 2.0 + 1e-9:
        filters.append("atempo=2.0")
        remaining /= 2.0
    if abs(remaining - 1.0) > 1e-6:
        filters.append(f"atempo={remaining:.6f}")
    return ",".join(filters)


_CAPTION_POSITIONS = {"top", "center", "bottom"}
_CAPTION_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
_CAPTION_FONT_RE = re.compile(r"^[A-Za-z0-9 _\-\u4e00-\u9fff()]{1,40}$")


def _caption_style(value: object) -> dict:
    """Normalize subtitle style fields into the canonical styling schema.

    The canonical shape covers the requirement set (font/color/outline/
    background/position) while still accepting the older handoff vocabulary
    (``size``/``stroke``) so existing data is never lost.
    """

    if not isinstance(value, dict):
        return {}
    clean: dict[str, object] = {}

    position = str(value.get("position") or "").strip().lower()
    if position in _CAPTION_POSITIONS:
        clean["position"] = position

    # The timeline supports free placement as a percentage of the
    # canvas. Keep the legacy top/center/bottom presets while persisting only
    # bounded public coordinates.
    for key in ("posX", "posY"):
        raw_position = value.get(key)
        if isinstance(raw_position, (int, float)) and math.isfinite(float(raw_position)):
            clean[key] = max(5.0, min(95.0, float(raw_position)))

    animation = value.get("animation")
    if isinstance(animation, (int, float)) and not isinstance(animation, bool) and math.isfinite(float(animation)):
        clean["animation"] = max(0.0, min(2.0, float(animation)))
    # The easing curve is independent of the fade duration: a cue may carry a
    # curve before any fade length is chosen.  Only non-linear curves are
    # persisted so untouched legacy captions keep their exact previous shape.
    easing = str(value.get("easing") or "").strip().lower()
    if easing in CAPTION_EASINGS and easing != "linear":
        clean["easing"] = easing
    elif isinstance(animation, (int, float)) and not isinstance(animation, bool) and math.isfinite(float(animation)):
        clean["easing"] = "linear"

    # Same convention as the curve: the default kind stays implicit.
    motion = str(value.get("motion") or "").strip().lower()
    if motion in CAPTION_MOTIONS and motion != "fade":
        clean["motion"] = motion

    color = _caption_hex_value(value.get("color") if "color" in value else value.get("primary"))
    if color:
        clean["color"] = color

    font = str(value.get("font") or "").strip()
    if font and _CAPTION_FONT_RE.fullmatch(font):
        clean["font"] = font

    size = value.get("fontSize", value.get("size"))
    if isinstance(size, (int, float)) and math.isfinite(float(size)):
        clean["fontSize"] = max(12, min(240, int(round(float(size)))))

    outline_color = value.get("outlineColor")
    if outline_color is None and isinstance(value.get("stroke"), str):
        outline_color = value["stroke"]
    outline_value = _caption_hex_value(outline_color)
    if outline_value:
        clean["outlineColor"] = outline_value

    outline_width = value.get("outlineWidth")
    if outline_width is None and isinstance(value.get("stroke"), (int, float)):
        outline_width = value["stroke"]
    if isinstance(outline_width, (int, float)) and math.isfinite(float(outline_width)):
        clean["outlineWidth"] = max(0, min(12, int(round(float(outline_width)))))
    if "outlineWidth" not in clean and "outlineColor" in clean:
        clean["outlineWidth"] = 4

    background = _caption_hex_value(value.get("background"))
    if background:
        clean["background"] = background
    bg_alpha = value.get("backgroundOpacity")
    if isinstance(bg_alpha, (int, float)) and math.isfinite(float(bg_alpha)):
        clean["backgroundOpacity"] = max(0.0, min(1.0, float(bg_alpha)))

    align = str(value.get("align") or "").strip().lower()
    if align in {"left", "center", "right"}:
        clean["align"] = align
    return clean


def _caption_hex_value(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not _CAPTION_COLOR_RE.fullmatch(text):
        return None
    if len(text) == 4:
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text


def _clip_duration(clip: object, version: object = None) -> float:
    """Best-effort duration lookup for a clip/version reference."""

    if not isinstance(clip, dict):
        return 1.0
    if isinstance(version, dict):
        for key in ("duration", "length"):
            value = _number(version.get(key), 0.0)
            if value > 0:
                return min(value, 24 * 60 * 60)
    for key in ("duration", "length"):
        value = _number(clip.get(key), 0.0)
        if value > 0:
            return min(value, 24 * 60 * 60)
    versions = clip.get("versions")
    if isinstance(versions, list):
        for item in reversed(versions):
            if isinstance(item, dict):
                value = _number(item.get("duration") or item.get("length"), 0.0)
                if value > 0:
                    return min(value, 24 * 60 * 60)
    # A short placeholder keeps a newly planned clip editable before a
    # generator reports media metadata.  The real duration is used after
    # delivery when the project is normalized again.
    return 1.0


def _clip_lookup(document: dict) -> dict[str, dict]:
    return {
        str(item.get("id")): item
        for item in (document.get("clips") or [])
        if isinstance(item, dict) and ID_RE.fullmatch(str(item.get("id") or ""))
    }


def _version_for_clip(clip: dict, version: object = None) -> dict | None:
    versions = clip.get("versions") if isinstance(clip, dict) else None
    if not isinstance(versions, list) or not versions:
        return None
    wanted = version
    if wanted is None:
        wanted = clip.get("current")
    try:
        wanted_int = int(wanted) if wanted is not None else None
    except (TypeError, ValueError, OverflowError):
        wanted_int = None
    if wanted_int is not None:
        for item in versions:
            if isinstance(item, dict) and item.get("v") == wanted_int:
                return item
    for item in reversed(versions):
        if isinstance(item, dict):
            return item
    return None


def _timeline_item(item: object, *, clip_lookup: dict[str, dict], track_id: str,
                   index: int = 0, media_lookup: dict[str, dict] | None = None,
                   document: dict | None = None) -> dict | None:
    """Normalize one user/legacy timeline item without trusting its fields."""

    if not isinstance(item, dict):
        return None
    clip_id = str(item.get("clip_id") or item.get("clip") or "").strip()
    media_id = str(item.get("media_id") or "").strip()
    media_lookup = media_lookup or {}
    if clip_id and ID_RE.fullmatch(clip_id) and clip_id in clip_lookup:
        clip = clip_lookup[clip_id]
    elif media_id and ID_RE.fullmatch(media_id) and media_id in media_lookup:
        # Audio tracks may reference a media-pool item rather than a generated
        # clip. Keep the public reference explicit so the renderer can resolve
        # it without guessing from a path supplied by the browser.
        clip = media_lookup[media_id]
        clip_id = ""
    else:
        return None
    version_raw = item.get("version")
    # An explicitly requested clip version must exist.  Falling back to the
    # current/latest version would make a stale editor reference silently
    # render a different asset.
    versions = clip.get("versions") if isinstance(clip, dict) else None
    if clip_id and version_raw is not None:
        try:
            requested_version = int(version_raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if not isinstance(versions, list) or not any(
                isinstance(value, dict) and value.get("v") == requested_version
                for value in versions):
            return None
    version_obj = _version_for_clip(clip, version_raw)
    version = version_obj.get("v") if isinstance(version_obj, dict) else version_raw
    try:
        version = int(version) if version is not None else None
    except (TypeError, ValueError, OverflowError):
        version = None
    duration_source = _clip_duration(clip, version_obj)
    raw_in = _number(item.get("in"), 0.0)
    raw_out = _number(item.get("out"), 0.0)
    raw_duration = _number(item.get("duration"), 0.0)
    declared_source_duration = _number(item.get("source_duration"), 0.0)
    # Older handoffs often omit duration on the version record.  Do not turn
    # an already-edited timeline into a one-second clip merely because the
    # manifest lacks that optional metadata; retain its bounded out point and
    # let the renderer/commit validation use the actual media when available.
    if raw_out > duration_source and isinstance(version_obj, dict):
        duration_source = min(raw_out, 24 * 60 * 60)
    if raw_duration > 0 and not version_obj:
        duration_source = min(raw_duration, 24 * 60 * 60)

    # Handoffs created before media metadata was recorded used a one-second
    # placeholder for a newly delivered clip.  Once the real file is
    # available, probe it here and carry the measured source length on the
    # timeline item.  The legacy placeholder is expanded only when it has the
    # unmistakable default shape (0s in, 1s out, 1s duration); an explicit trim
    # is preserved after the item carries ``source_duration``.
    measured_source_duration = 0.0
    probe_placeholder = (
        track_id == "audio-main" and media_id and raw_in <= 0.000001
        and 0 < raw_out <= 1.000001 and abs(raw_duration - raw_out) <= 0.01
        and declared_source_duration <= 1.01
    )
    if (clip_id or media_id) and (not declared_source_duration or probe_placeholder) and isinstance(document, dict):
        try:
            measured_source_duration = _timeline_source_duration(
                document, {"clip_id": clip_id, "media_id": media_id, "version": version}
            )
        except (NameError, OSError, ValueError, TypeError):
            measured_source_duration = 0.0
    if measured_source_duration > 0:
        duration_source = max(duration_source, min(measured_source_duration, 24 * 60 * 60))
    elif declared_source_duration > 0:
        duration_source = max(duration_source, min(declared_source_duration, 24 * 60 * 60))

    # Audio media inserted by older Studio builds could be persisted with a
    # synthetic one-second source_duration/out even when the actual file was
    # longer. Treat that unmistakable audio placeholder like the legacy video
    # placeholder and expand it to the probed source length. Explicit trims
    # carrying a real source_duration greater than one second remain intact.
    legacy_placeholder = (
        raw_in <= 0.000001
        and 0 < raw_out <= 1.000001
        and abs(raw_duration - raw_out) <= 0.01
        and duration_source > raw_out + 0.05
        and (not declared_source_duration or (track_id == "audio-main" and declared_source_duration <= 1.01))
    )
    if legacy_placeholder:
        raw_out = duration_source

    start = max(0.0, min(_number(item.get("start"), 0.0), 24 * 60 * 60))
    in_point = max(0.0, min(raw_in, duration_source))
    if in_point >= duration_source:
        in_point = max(0.0, duration_source - 0.001)
    # Use the normalized value above so a legacy one-second placeholder that
    # was expanded to the measured source duration is actually reflected in
    # the canonical timeline response.
    out_point = raw_out if raw_out > 0 else duration_source
    if out_point <= in_point:
        out_point = duration_source
    out_point = min(duration_source, max(in_point + 0.001, min(out_point, duration_source)))
    if out_point <= in_point:
        return None
    speed = _number(item.get("speed"), 1.0)
    if speed <= 0:
        speed = 1.0
    speed = max(0.25, min(speed, 4.0))
    duration = max(0.001, (out_point - in_point) / speed)
    raw_id = str(item.get("id") or "").strip()
    ident = raw_id if ID_RE.fullmatch(raw_id) else stable_id(
        "tl", track_id, clip_id or media_id, version or "latest", index, start, in_point, out_point
    )
    transform_raw = item.get("transform") if isinstance(item.get("transform"), dict) else {}
    transform = {
        "x": max(-10000.0, min(10000.0, _number(transform_raw.get("x"), 0.0))),
        "y": max(-10000.0, min(10000.0, _number(transform_raw.get("y"), 0.0))),
        "scale": max(0.05, min(20.0, _number(transform_raw.get("scale"), 1.0))),
        "rotate": max(-3600.0, min(3600.0, _number(transform_raw.get("rotate"), 0.0))),
        "opacity": max(0.05, min(1.0, _number(transform_raw.get("opacity"), 1.0))),
        "border": max(0.0, min(12.0, _number(transform_raw.get("border"), 0.0))),
    }
    link_group = str(item.get("link_group") or "").strip()
    if link_group and not ID_RE.fullmatch(link_group):
        link_group = ""

    def transition(value: object) -> dict:
        raw = value if isinstance(value, dict) else {}
        kind = str(raw.get("kind") or "cut").lower()
        if kind not in TIMELINE_TRANSITIONS:
            kind = "cut"
        default = 0.3 if kind in {"fade", "dip_black", "dip_white"} else 0.0
        amount = max(0.0, min(2.0, _number(raw.get("duration"), default)))
        normalized = {"kind": kind, "duration": amount}
        # Easing is only written when non-linear so existing projects keep
        # byte-identical normalized transitions.
        easing = str(raw.get("easing") or "linear").lower()
        if easing in TIMELINE_EASINGS and easing != "linear":
            normalized["easing"] = easing
        return normalized

    return {
        "id": ident,
        "clip_id": clip_id or None,
        "media_id": media_id or None,
        "version": version,
        "source_duration": round(duration_source, 6),
        "start": round(start, 6),
        "in": round(in_point, 6),
        "out": round(out_point, 6),
        "duration": round(duration, 6),
        "speed": round(speed, 6),
        "transform": transform,
        "link_group": link_group or None,
        "transition_in": transition(item.get("transition_in")),
        "transition_out": transition(item.get("transition_out")),
        "gain": max(-60.0, min(24.0, _number(item.get("gain"), 0.0))),
        "fade_in": max(0.0, min(30.0, _number(item.get("fade_in"), 0.0))),
        "fade_out": max(0.0, min(30.0, _number(item.get("fade_out"), 0.0))),
        "detach": bool(item.get("detach", False)),
    }


def _clamp_timeline_transitions(timeline: dict) -> dict:
    """Clamp adjacent fade transitions to the usable neighboring durations.

    A fade is a cross-dissolve between two adjacent clips.  Edge fades have no
    neighbor and therefore become a cut; this prevents a render from silently
    inventing an implicit fade at the beginning or end of a track.
    """

    for track in timeline.get("tracks", []):
        if not isinstance(track, dict) or track.get("kind") != "video":
            continue
        clips = track.get("clips") if isinstance(track.get("clips"), list) else []
        clips = [item for item in clips if isinstance(item, dict)]
        clips.sort(key=lambda item: (item.get("start", 0.0), str(item.get("id"))))
        # A seam has one canonical transition owner: the clip on its left.
        # Older clients occasionally wrote the same transition on the right
        # clip's ``transition_in`` field.  Resolve that legacy form before
        # validating so it is rendered instead of silently ignored, and clear
        # the duplicate edge to keep future writes deterministic.
        for index in range(len(clips) - 1):
            left, right = clips[index], clips[index + 1]
            left_out = left.get("transition_out") if isinstance(left.get("transition_out"), dict) else {}
            right_in = right.get("transition_in") if isinstance(right.get("transition_in"), dict) else {}
            out_kind = str(left_out.get("kind") or "cut").lower()
            in_kind = str(right_in.get("kind") or "cut").lower()
            if out_kind in {"fade", "dip_black", "dip_white"}:
                chosen = left_out
            elif in_kind in {"fade", "dip_black", "dip_white"}:
                chosen = right_in
            else:
                continue
            left["transition_out"] = dict(chosen)
            right["transition_in"] = {"kind": "cut", "duration": 0.0}
        for index, item in enumerate(clips):
            for edge in ("in", "out"):
                key = f"transition_{edge}"
                transition = item.get(key) if isinstance(item.get(key), dict) else {}
                kind = str(transition.get("kind") or "cut").lower()
                if kind not in {"fade", "dip_black", "dip_white"}:
                    item[key] = {"kind": "cut", "duration": 0.0}
                    continue
                neighbor = clips[index - 1] if edge == "in" and index > 0 else (
                    clips[index + 1] if edge == "out" and index + 1 < len(clips) else None
                )
                if neighbor is None:
                    item[key] = {"kind": "cut", "duration": 0.0}
                    continue
                # A cross-dissolve is defined for a seam, not for arbitrary
                # timeline overlap.  Keep the same small seam tolerance used
                # by the Studio pairing logic (50 ms) so stale/legacy data
                # cannot leave a transition attached to clips that are far
                # apart or massively overlapped.  The transition window stays cut-centred
                # while remaining fail-safe:
                # invalid placement is normalized to a cut instead of
                # rejecting the whole project.
                if edge == "out":
                    gap = _number(neighbor.get("start"), 0.0) - (
                        _number(item.get("start"), 0.0) + _number(item.get("duration"), 0.0))
                else:
                    gap = _number(item.get("start"), 0.0) - (
                        _number(neighbor.get("start"), 0.0) + _number(neighbor.get("duration"), 0.0))
                if abs(gap) > 0.05:
                    item[key] = {"kind": "cut", "duration": 0.0}
                    continue
                usable = min(
                    2.0,
                    max(0.0, _number(item.get("duration"), 0.0)) / 2.0,
                    max(0.0, _number(neighbor.get("duration"), 0.0)) / 2.0,
                )
                amount = max(0.0, min(usable, _number(transition.get("duration"), 0.3)))
                clamped = {"kind": kind, "duration": round(amount, 6)}
                easing = str(transition.get("easing") or "linear").lower()
                if easing in TIMELINE_EASINGS and easing != "linear":
                    clamped["easing"] = easing
                item[key] = clamped
    return timeline


def normalize_timeline(document: dict, assembly: dict | None = None) -> dict:
    """Return a bounded canonical timeline, migrating ``assembly.order``.

    This function is intentionally side-effect free.  Legacy projects become
    editable immediately in memory; a revision is only written on an explicit
    timeline commit.
    """

    assembly = assembly if isinstance(assembly, dict) else {}
    clip_lookup = _clip_lookup(document)
    raw = assembly.get("timeline") if isinstance(assembly.get("timeline"), dict) else {}
    canvas_raw = raw.get("canvas") if isinstance(raw.get("canvas"), dict) else {}
    ratio = str(canvas_raw.get("ratio") or (document.get("presets") or {}).get("ratio") or "16:9")
    if ratio not in TIMELINE_RATIOS:
        ratio = "9:16"
    default_width, default_height = TIMELINE_RATIOS[ratio]
    width = int(max(64, min(4096, _number(canvas_raw.get("width"), default_width))))
    height = int(max(64, min(4096, _number(canvas_raw.get("height"), default_height))))
    tracks_raw = raw.get("tracks") if isinstance(raw.get("tracks"), list) else None
    has_canonical_main = False
    track_map: dict[str, dict] = {}
    if tracks_raw is not None:
        for raw_track in tracks_raw[:TIMELINE_MAX_TRACKS]:
            if not isinstance(raw_track, dict):
                continue
            track_id = str(raw_track.get("id") or "").strip()
            kind = str(raw_track.get("kind") or "").lower()
            if track_id not in TIMELINE_TRACKS or TIMELINE_TRACKS[track_id] != kind:
                continue
            if track_id in track_map:
                continue
            if track_id == "video-main":
                has_canonical_main = True
            track_map[track_id] = {"id": track_id, "kind": kind, "clips": [], "cues": [],
                                   "muted": bool(raw_track.get("muted", False)),
                                   "hidden": bool(raw_track.get("hidden", False))}
            values = raw_track.get("clips") if isinstance(raw_track.get("clips"), list) else []
            for index, candidate in enumerate(values[:TIMELINE_MAX_ITEMS]):
                normalized = _timeline_item(candidate, clip_lookup=clip_lookup,
                                            track_id=track_id, index=index,
                                            media_lookup={str(item.get("id")): item for item in (document.get("asset", {}).get("media", []) or []) if isinstance(item, dict)},
                                            document=document)
                if normalized is not None and kind in {"video", "audio"}:
                    track_map[track_id]["clips"].append(normalized)
            if kind == "subtitle":
                cues = raw_track.get("cues") if isinstance(raw_track.get("cues"), list) else []
                for cue in cues[:TIMELINE_MAX_CUES]:
                    if not isinstance(cue, dict):
                        continue
                    text_value = str(cue.get("text") or "").strip()[:2000]
                    if not text_value or _looks_private(text_value):
                        continue
                    start = max(0.0, min(24 * 60 * 60, _number(cue.get("start"), 0.0)))
                    end = max(start + 0.001, min(24 * 60 * 60, _number(cue.get("end"), start + 1.0)))
                    cue_id = str(cue.get("id") or "").strip()
                    if not ID_RE.fullmatch(cue_id):
                        cue_id = stable_id("cue", track_id, start, end, text_value)
                    cue_kind = str(cue.get("kind") or "subtitle").strip().lower()
                    if cue_kind not in {"subtitle", "title"}:
                        cue_kind = "subtitle"
                    track_map[track_id]["cues"].append({
                        "id": cue_id, "start": round(start, 6), "end": round(end, 6),
                        "text": text_value, "kind": cue_kind,
                        "style": _caption_style(cue.get("style")),
                    })

    # Always expose the four known tracks in a stable order.  If no canonical
    # timeline existed, migrate the old primary order to ``video-main``.
    order = assembly.get("order") if isinstance(assembly.get("order"), list) else []
    if tracks_raw is None or not has_canonical_main:
        migrated: list[dict] = []
        for index, clip_id in enumerate(order[:TIMELINE_MAX_ITEMS]):
            candidate = {"clip_id": str(clip_id), "start": 0.0}
            normalized = _timeline_item(candidate, clip_lookup=clip_lookup,
                                        track_id="video-main", index=index,
                                        document=document)
            if normalized is not None:
                if migrated:
                    normalized["start"] = round(migrated[-1]["start"] + migrated[-1]["duration"], 6)
                migrated.append(normalized)
        track_map["video-main"] = {"id": "video-main", "kind": "video", "clips": migrated, "cues": [],
                                    "muted": False, "hidden": False}

    tracks: list[dict] = []
    for track_id, kind in TIMELINE_TRACKS.items():
        current = track_map.get(track_id) or {"id": track_id, "kind": kind, "clips": [], "cues": [],
                                              "muted": False, "hidden": False}
        entry = {"id": track_id, "kind": kind,
                 "muted": bool(current.get("muted", False)),
                 "hidden": bool(current.get("hidden", False))}
        if kind == "subtitle":
            entry["cues"] = current.get("cues", [])[:TIMELINE_MAX_CUES]
        else:
            clips = current.get("clips", []) if isinstance(current.get("clips"), list) else []
            clips = [item for item in clips if isinstance(item, dict)][:TIMELINE_MAX_ITEMS]
            clips.sort(key=lambda item: (item.get("start", 0.0), str(item.get("id"))))
            entry["clips"] = clips
        tracks.append(entry)
    zoom = max(0.25, min(64.0, _number(raw.get("zoom"), 1.0)))
    # Keep the editable canvas independent from the content's actual end.
    # The timeline workspace is persistent so a short clip does not collapse
    # the ruler/scroll area.  The manager uses a smaller 7s minimum
    # (suitable for short-form clips) while still expanding for later media.
    workspace_duration = _number(
        raw.get("workspace_duration", raw.get("workspaceDuration", raw.get("duration"))),
        7.0,
    )
    workspace_duration = max(7.0, min(24 * 60 * 60, workspace_duration))
    # Grow the workspace when a user places content beyond the current canvas;
    # never shrink it just because a short clip was removed.
    content_end = 0.0
    for track in track_map.values():
        for item in (track.get("clips") or []):
            content_end = max(
                content_end,
                _number(item.get("start"), 0.0) + _number(item.get("duration"), 0.0),
            )
        for cue in (track.get("cues") or []):
            content_end = max(content_end, _number(cue.get("end"), 0.0))
    workspace_duration = max(workspace_duration, min(24 * 60 * 60, content_end))
    audio_raw = raw.get("audio") if isinstance(raw.get("audio"), dict) else assembly.get("audio")
    if not isinstance(audio_raw, dict):
        audio_raw = {}
    audio = {
        "bgm": norm_rel(audio_raw.get("bgm")) if audio_raw.get("bgm") else None,
        "bgm_gain": max(-60.0, min(24.0, _number(audio_raw.get("bgm_gain"), 0.0))),
        "mute_original": bool(audio_raw.get("mute_original", False)),
    }
    background = str(canvas_raw.get("background") or "#000000").strip().lower()
    if not HEX_COLOR_RE.fullmatch(background):
        background = "#000000"
    fps_value = raw.get("fps", raw.get("frame_rate", 30))
    try:
        fps = int(fps_value)
    except (TypeError, ValueError, OverflowError):
        fps = 30
    if fps not in TIMELINE_FPS:
        fps = 30
    timeline = {
        "schema": 2,
        "canvas": {"ratio": ratio, "width": width, "height": height, "background": background},
        "fps": fps,
        "zoom": round(zoom, 4),
        "workspace_duration": round(workspace_duration, 6),
        "snap": bool(raw.get("snap", True)),
        "audio": audio,
        "tracks": tracks,
    }
    return _clamp_timeline_transitions(timeline)


def _timeline_tracks(timeline: dict) -> dict[str, dict]:
    return {
        str(track.get("id")): track
        for track in timeline.get("tracks", [])
        if isinstance(track, dict) and str(track.get("id")) in TIMELINE_TRACKS
    }


def _timeline_fade_error(timeline: dict) -> str | None:
    """Validate fade adjacency, edge behavior and usable duration limits."""

    for track in timeline.get("tracks", []) if isinstance(timeline, dict) else []:
        if not isinstance(track, dict) or track.get("kind") != "video":
            continue
        clips = track.get("clips") if isinstance(track.get("clips"), list) else []
        clips = [item for item in clips if isinstance(item, dict)]
        clips.sort(key=lambda item: (_number(item.get("start"), 0.0), str(item.get("id"))))
        for index, item in enumerate(clips):
            for edge in ("in", "out"):
                transition = item.get(f"transition_{edge}")
                if not isinstance(transition, dict):
                    continue
                kind = str(transition.get("kind") or "cut").lower()
                duration = _number(transition.get("duration"), -1.0)
                easing = str(transition.get("easing") or "linear").lower()
                if kind not in TIMELINE_TRANSITIONS or duration < 0 or easing not in TIMELINE_EASINGS:
                    return "invalid transition"
                if kind not in {"fade", "dip_black", "dip_white"}:
                    if duration > 0.000001:
                        return "cut transition duration must be zero"
                    continue
                neighbor = clips[index - 1] if edge == "in" and index > 0 else (
                    clips[index + 1] if edge == "out" and index + 1 < len(clips) else None
                )
                if neighbor is None:
                    return "edge fade requires an adjacent clip"
                if edge == "out":
                    boundary = _number(item.get("start"), 0.0) + _number(item.get("duration"), 0.0)
                    neighbor_start = _number(neighbor.get("start"), 0.0)
                    gap = neighbor_start - boundary
                else:
                    boundary = _number(neighbor.get("start"), 0.0) + _number(neighbor.get("duration"), 0.0)
                    item_start = _number(item.get("start"), 0.0)
                    gap = item_start - boundary
                # Match the canonical seam tolerance used by normalization:
                # a transition belongs to a cut-centered window, allowing a
                # tiny placement error but not an arbitrary gap/overlap.
                if abs(gap) > 0.05:
                    return "fade requires touching or overlapping clips"
                usable = min(
                    2.0,
                    max(0.0, _number(item.get("duration"), 0.0)) / 2.0,
                    max(0.0, _number(neighbor.get("duration"), 0.0)) / 2.0,
                )
                if duration <= 0:
                    return "fade duration must be greater than zero"
                # The editor accepts a requested duration and the normalizer
                # clamps it to the two-sided usable duration.  Do not reject a
                # valid edit merely because a stale client sent the old
                # default (for example 0.3s after a short trim); the operation
                # path below normalizes it before the final project write.
                if duration > usable + 0.000001:
                    return "fade duration exceeds usable clip duration"
    return None


def validate_timeline(document: dict, timeline: object) -> tuple[bool, str | None]:
    if not isinstance(timeline, dict):
        return False, "invalid timeline"
    fps_value = timeline.get("fps", 30)
    if isinstance(fps_value, bool) or not isinstance(fps_value, (int, float)) or int(fps_value) not in TIMELINE_FPS:
        return False, "invalid timeline fps"
    canvas_value = timeline.get("canvas") if isinstance(timeline.get("canvas"), dict) else {}
    if not HEX_COLOR_RE.fullmatch(str(canvas_value.get("background") or "#000000")):
        return False, "invalid canvas background"
    raw_tracks = timeline.get("tracks")
    if not isinstance(raw_tracks, list) or len(raw_tracks) != len(TIMELINE_TRACKS):
        return False, "invalid timeline tracks"
    clip_lookup = _clip_lookup(document)
    media_lookup = {
        str(item.get("id")): item
        for item in (document.get("asset", {}).get("media", []) or [])
        if isinstance(item, dict)
    }
    seen_track_ids: set[str] = set()
    for raw_track in raw_tracks:
        if not isinstance(raw_track, dict):
            return False, "invalid timeline track"
        track_id = str(raw_track.get("id") or "")
        kind = str(raw_track.get("kind") or "").lower()
        if track_id not in TIMELINE_TRACKS or TIMELINE_TRACKS[track_id] != kind or track_id in seen_track_ids:
            return False, "invalid timeline track"
        seen_track_ids.add(track_id)
        if not isinstance(raw_track.get("muted", False), bool) or not isinstance(raw_track.get("hidden", False), bool):
            return False, "invalid track state"
        if kind == "subtitle":
            cues = raw_track.get("cues")
            if not isinstance(cues, list) or len(cues) > TIMELINE_MAX_CUES:
                return False, "invalid subtitle cues"
            cue_ids: set[str] = set()
            for cue in cues:
                if not isinstance(cue, dict):
                    return False, "invalid subtitle cue"
                cue_id = str(cue.get("id") or "")
                if not ID_RE.fullmatch(cue_id) or cue_id in cue_ids:
                    return False, "invalid subtitle cue id"
                cue_ids.add(cue_id)
                text_value = str(cue.get("text") or "").strip()
                if not text_value or len(text_value) > 2000 or _looks_private(text_value):
                    return False, "invalid subtitle text"
                start = _number(cue.get("start"), -1.0)
                end = _number(cue.get("end"), -1.0)
                if start < 0 or end <= start or end > 24 * 60 * 60:
                    return False, "invalid subtitle range"
                cue_kind = str(cue.get("kind") or "subtitle").strip().lower()
                if cue_kind not in {"subtitle", "title"}:
                    return False, "invalid subtitle kind"
                cue_style = cue.get("style")
                if cue_style is not None and not isinstance(cue_style, dict):
                    return False, "invalid subtitle style"
                if isinstance(cue_style, dict):
                    # A bounded re-normalization also validates the values:
                    # unknown/malformed fields are dropped, never accepted.
                    renormalized = _caption_style(cue_style)
                    for key in ("color", "outlineColor", "background", "font", "fontSize",
                                "outlineWidth", "backgroundOpacity", "position", "posX", "posY", "animation", "easing", "motion"):
                        if key in cue_style and key not in renormalized:
                            if key in {"color", "outlineColor", "background", "font", "position", "fontSize",
                                       "outlineWidth", "backgroundOpacity", "posX", "posY", "animation", "easing", "motion"}:
                                return False, "invalid subtitle style"
            continue
        items = raw_track.get("clips")
        if not isinstance(items, list) or len(items) > TIMELINE_MAX_ITEMS:
            return False, "invalid timeline clips"
        for raw_item in items:
            if not isinstance(raw_item, dict):
                return False, "invalid timeline item"
            clip_id = str(raw_item.get("clip_id") or "").strip()
            media_id = str(raw_item.get("media_id") or "").strip()
            if kind == "video":
                if not clip_id or media_id or clip_id not in clip_lookup:
                    return False, "video track requires a valid clip reference"
            elif kind == "audio":
                if clip_id and media_id:
                    return False, "audio item cannot contain both clip and media references"
                if clip_id and clip_id not in clip_lookup:
                    return False, "audio track references unknown clip"
                if media_id:
                    media = media_lookup.get(media_id)
                    if media is None or str(media.get("kind") or "").lower() not in {"audio", "video"}:
                        return False, "audio track references invalid media"
                if not clip_id and not media_id:
                    return False, "audio track requires a media reference"
            raw_item_id = str(raw_item.get("id") or "")
            if not ID_RE.fullmatch(raw_item_id):
                return False, "invalid timeline item id"
            for key in ("start", "in", "out", "duration", "speed"):
                raw_value = raw_item.get(key)
                if isinstance(raw_value, bool):
                    return False, "invalid timeline timing"
                numeric = _number(raw_value, -1.0)
                if numeric < 0 or not math.isfinite(numeric):
                    return False, "invalid timeline timing"
            if _number(raw_item.get("out"), 0.0) <= _number(raw_item.get("in"), 0.0):
                return False, "invalid timeline range"
            if _number(raw_item.get("duration"), 0.0) <= 0:
                return False, "invalid timeline duration"
            for edge in ("transition_in", "transition_out"):
                raw_transition = raw_item.get(edge)
                if not isinstance(raw_transition, dict):
                    return False, "invalid transition"
                raw_kind = str(raw_transition.get("kind") or "").lower()
                raw_duration = _number(raw_transition.get("duration"), -1.0)
                raw_easing = str(raw_transition.get("easing") or "linear").lower()
                if raw_kind not in TIMELINE_TRANSITIONS or raw_duration < 0 or raw_easing not in TIMELINE_EASINGS:
                    return False, "invalid transition"
            link_group = raw_item.get("link_group")
            if link_group is not None and (not isinstance(link_group, str) or (link_group and not ID_RE.fullmatch(link_group))):
                return False, "invalid link group"
            version = raw_item.get("version")
            if clip_id and version is not None:
                clip = clip_lookup.get(clip_id)
                versions = clip.get("versions") if isinstance(clip, dict) else None
                try:
                    version_number = int(version)
                except (TypeError, ValueError, OverflowError):
                    return False, "invalid media version"
                if not isinstance(versions, list) or not any(
                        isinstance(value, dict) and value.get("v") == version_number
                        for value in versions):
                    return False, "media version not found"
    fade_error = _timeline_fade_error(timeline)
    if fade_error:
        return False, fade_error
    normalized = normalize_timeline(document, {"timeline": timeline, "order": []})
    tracks = _timeline_tracks(normalized)
    if len(tracks) != len(TIMELINE_TRACKS):
        return False, "invalid timeline tracks"
    seen: set[str] = set()
    clip_ids = set(_clip_lookup(document))
    media_ids = {
        str(item.get("id"))
        for item in (document.get("asset", {}).get("media", []) or [])
        if isinstance(item, dict) and ID_RE.fullmatch(str(item.get("id") or ""))
    }
    for track_id, track in tracks.items():
        kind = TIMELINE_TRACKS[track_id]
        if track.get("kind") != kind:
            return False, "invalid timeline track kind"
        if kind == "subtitle":
            for cue in track.get("cues", []):
                if not isinstance(cue, dict) or not ID_RE.fullmatch(str(cue.get("id") or "")):
                    return False, "invalid subtitle cue"
            continue
        for item in track.get("clips", []):
            if not isinstance(item, dict) or not ID_RE.fullmatch(str(item.get("id") or "")):
                return False, "invalid timeline item id"
            if item["id"] in seen:
                return False, "duplicate timeline item id"
            seen.add(item["id"])
            if item.get("clip_id") not in clip_ids and item.get("media_id") not in media_ids:
                return False, "timeline references unknown media"
            for key in ("start", "in", "out", "duration", "speed"):
                value = _number(item.get(key), -1.0)
                if value < 0 or not math.isfinite(value):
                    return False, "invalid timeline timing"
            if item.get("out", 0) <= item.get("in", 0) or item.get("duration", 0) <= 0:
                return False, "invalid timeline range"
            for edge in ("transition_in", "transition_out"):
                transition = item.get(edge) or {}
                if transition.get("kind") not in TIMELINE_TRANSITIONS:
                    return False, "invalid transition"
                if _number(transition.get("duration"), -1) < 0:
                    return False, "invalid transition duration"
                if str(transition.get("easing") or "linear").lower() not in TIMELINE_EASINGS:
                    return False, "invalid transition"
    return True, None


def timeline_projection(document: dict) -> dict:
    assembly = document.get("assembly") if isinstance(document.get("assembly"), dict) else {}
    timeline = normalize_timeline(document, assembly)
    # ``order`` is deliberately derived from the primary track, preserving the
    # old recorder/UI projection for clients that do not understand timelines.
    main = next((track for track in timeline["tracks"] if track["id"] == "video-main"), None)
    order = [item["clip_id"] for item in (main or {}).get("clips", [])]
    return {"timeline": timeline, "order": order}


def _timeline_item_list(timeline: dict, track_id: str) -> list[dict]:
    track = _timeline_tracks(timeline).get(track_id)
    if not isinstance(track, dict):
        return []
    values = track.get("clips") if isinstance(track.get("clips"), list) else []
    return values


def _find_timeline_item(timeline: dict, item_id: str) -> tuple[dict | None, dict | None, int]:
    for track in timeline.get("tracks", []):
        if not isinstance(track, dict):
            continue
        values = track.get("clips") if isinstance(track.get("clips"), list) else []
        for index, item in enumerate(values):
            if isinstance(item, dict) and item.get("id") == item_id:
                return track, item, index
    return None, None, -1


def _find_timeline_cue(timeline: dict, cue_id: str) -> tuple[dict | None, dict | None, int]:
    """Find a subtitle cue without treating it as a media clip."""

    for track in timeline.get("tracks", []):
        if not isinstance(track, dict) or track.get("kind") != "subtitle":
            continue
        cues = track.get("cues") if isinstance(track.get("cues"), list) else []
        for index, cue in enumerate(cues):
            if isinstance(cue, dict) and str(cue.get("id")) == cue_id:
                return track, cue, index
    return None, None, -1


def _replace_timeline_item(document: dict, item: dict, track_id: str, index: int) -> dict | None:
    media_lookup = {
        str(entry.get("id")): entry
        for entry in (document.get("asset", {}).get("media", []) or [])
        if isinstance(entry, dict)
    }
    return _timeline_item(item, clip_lookup=_clip_lookup(document), track_id=track_id,
                          index=index, media_lookup=media_lookup, document=document)


def _timeline_source_duration(document: dict, item: dict) -> float:
    """Return the real bounded source duration for an editable timeline item."""

    if not isinstance(item, dict):
        return 1.0
    clip_id = str(item.get("clip_id") or "").strip()
    rel: str | None = None
    if clip_id:
        clip = _clip_lookup(document).get(clip_id)
        version = _version_for_clip(clip or {}, item.get("version")) if clip else None
        fallback = _clip_duration(clip or {}, version)
        if isinstance(version, dict):
            rel = version.get("file")
        if not rel and clip:
            rel = clip_media(document, clip_id, False)
    else:
        media_id = str(item.get("media_id") or "").strip()
        media = next((entry for entry in (document.get("asset", {}).get("media", []) or [])
                      if isinstance(entry, dict) and str(entry.get("id")) == media_id), None)
        fallback = _number((media or {}).get("duration"), 1.0)
        rel = (media or {}).get("file") if isinstance(media, dict) else None
    slug = str(document.get("slug") or "").strip()
    source = safe_file(slug, rel) if slug and rel else None
    probed = _probe_media_duration(source) if source else 0.0
    return max(0.001, probed or fallback)


def _timeline_canvas(value: object, current: dict) -> dict | None:
    if not isinstance(value, dict):
        return None
    ratio = str(value.get("ratio") or current.get("ratio") or "9:16")
    if ratio not in TIMELINE_RATIOS:
        return None
    defaults = TIMELINE_RATIOS[ratio]
    width = _number(value.get("width"), defaults[0])
    height = _number(value.get("height"), defaults[1])
    if width < 64 or height < 64 or width > 4096 or height > 4096:
        return None
    background = str(value.get("background") or current.get("background") or "#000000").strip()
    if not HEX_COLOR_RE.fullmatch(background):
        return None
    return {"ratio": ratio, "width": int(width), "height": int(height), "background": background.lower()}


def _apply_lane_transform_default(item: dict, destination: str) -> None:
    """Re-apply the lane default when a move changes track.

    The web editor creates a clip on the main video track full frame (scale 1)
    and a clip on an overlay lane as a 0.35 inset.  A move that changes track
    used to carry the old transform along, so a picture-in-picture dropped on
    VIDEO 1 stayed a small floating picture.  Only the geometry that makes the
    inset is written; opacity and border are left as the user set them.  Moves
    that stay on the same track (a time nudge) keep the transform untouched.
    """
    if not isinstance(item, dict):
        return
    raw = item.get("transform") if isinstance(item.get("transform"), dict) else {}
    x = _number(raw.get("x"), 0.0)
    y = _number(raw.get("y"), 0.0)
    scale = _number(raw.get("scale"), 1.0)
    rotate = _number(raw.get("rotate"), 0.0)
    full_frame = abs(scale - 1.0) <= 0.001 and abs(x) <= 0.001 and abs(y) <= 0.001 and abs(rotate) <= 0.001
    if str(destination) == "video-main":
        if full_frame:
            return
        item["transform"] = {**raw, "x": 0.0, "y": 0.0, "scale": 1.0, "rotate": 0.0}
        return
    if full_frame:
        item["transform"] = {**raw, "scale": 0.35}


def apply_timeline_operations(document: dict, timeline: dict, operations: object) -> tuple[dict | None, str | None]:
    """Apply a bounded operation batch to a canonical timeline.

    The function never mutates the caller's timeline on error. Undo/redo are
    batch-local so the manifest does not accumulate editor history snapshots.
    """

    if not isinstance(operations, list) or len(operations) > TIMELINE_MAX_OPS:
        return None, "invalid operations"
    working = copy.deepcopy(timeline)
    history: list[dict] = []
    redo: list[dict] = []
    clip_lookup = _clip_lookup(document)
    media_lookup = {
        str(entry.get("id")): entry
        for entry in (document.get("asset", {}).get("media", []) or [])
        if isinstance(entry, dict)
    }

    def fail(message: str) -> tuple[None, str]:
        return None, message

    def linked_items(group: str) -> list[tuple[dict, dict, int]]:
        """Return all non-subtitle timeline items in a link group.

        Link groups are intentionally limited to video/audio items.  Keeping
        this lookup local to the working copy makes grouped edits atomic with
        the surrounding operation batch.
        """
        if not group:
            return []
        found: list[tuple[dict, dict, int]] = []
        for candidate_track in working.get("tracks", []):
            if not isinstance(candidate_track, dict) or candidate_track.get("kind") not in {"video", "audio"}:
                continue
            for candidate_index, candidate in enumerate(candidate_track.get("clips", []) or []):
                if isinstance(candidate, dict) and str(candidate.get("link_group") or "") == group:
                    found.append((candidate_track, candidate, candidate_index))
        return found

    def linked_group_for(item: dict) -> str:
        return str(item.get("link_group") or "").strip()

    for raw in operations:
        if not isinstance(raw, dict):
            return fail("invalid operation")
        op_name = str(raw.get("op") or raw.get("type") or "").strip().lower()
        if op_name in {"undo", "redo"}:
            if op_name == "undo" and history:
                redo.append(copy.deepcopy(working))
                working = history.pop()
            elif op_name == "redo" and redo:
                history.append(copy.deepcopy(working))
                working = redo.pop()
            continue
        if op_name == "replace_timeline":
            candidate = raw.get("timeline")
            if not isinstance(candidate, dict):
                return fail("invalid replacement timeline")
            # A full-timeline save can come from an older/stale inspector. Do
            # the same safe transition normalization used by single-field
            # edits before validation, preventing an otherwise valid save from
            # being rejected for a now-impossible fade duration.
            candidate = copy.deepcopy(candidate)
            _clamp_timeline_transitions(candidate)
            # Validate the submitted references before normalization; otherwise
            # an unknown clip/version could be silently dropped by the legacy
            # migration path and appear as a successful empty replacement.
            valid, error = validate_timeline(document, candidate)
            if not valid:
                return fail(error or "invalid replacement timeline")
            normalized = normalize_timeline(document, {"timeline": candidate, "order": []})
            history.append(copy.deepcopy(working))
            working = normalized
            redo.clear()
            continue

        before = copy.deepcopy(working)
        tracks = _timeline_tracks(working)
        item_id = str(raw.get("item_id") or raw.get("id") or "").strip()
        track_id = str(raw.get("track_id") or "video-main").strip()
        if op_name == "add":
            if track_id not in TIMELINE_TRACKS or TIMELINE_TRACKS[track_id] not in {"video", "audio"}:
                return fail("invalid timeline track")
            clip_id = str(raw.get("clip_id") or raw.get("clip") or "").strip()
            media_id = str(raw.get("media_id") or "").strip()
            if clip_id and (clip_id not in clip_lookup or not ID_RE.fullmatch(clip_id)):
                return fail("unknown clip")
            if not clip_id and (media_id not in media_lookup or not ID_RE.fullmatch(media_id)):
                return fail("unknown media")
            source = {"clip_id": clip_id or None, "media_id": media_id or None,
                      "version": raw.get("version"), "start": raw.get("start", 0)}
            # Preserve the caller's measured media duration for add operations.
            # Media-pool records may omit duration in the manifest; without
            # this hint normalization falls back to the historical 1s
            # placeholder even when the file is a longer soundtrack.
            if raw.get("in") is not None:
                source["in"] = raw.get("in")
            if raw.get("out") is not None:
                source["out"] = raw.get("out")
            elif raw.get("duration") is not None:
                source["out"] = _number(raw.get("in"), 0.0) + _number(raw.get("duration"), 0.0)
            if raw.get("duration") is not None:
                source["duration"] = raw.get("duration")
            item = _timeline_item(source, clip_lookup=clip_lookup, track_id=track_id,
                                  index=len(_timeline_item_list(working, track_id)),
                                  media_lookup=media_lookup, document=document)
            if item is None:
                return fail("unable to add timeline item")
            if item_id:
                if not ID_RE.fullmatch(item_id):
                    return fail("invalid timeline item id")
                item["id"] = item_id
            items = _timeline_item_list(working, track_id)
            if len(items) >= TIMELINE_MAX_ITEMS:
                return fail("timeline item limit exceeded")
            items.append(item)
        elif op_name == "remove":
            track, item, index = _find_timeline_item(working, item_id)
            if item is not None and track is not None:
                group = linked_group_for(item)
                if group:
                    # A linked A/V selection is one logical edit unit: remove
                    # every member, not just the item whose button was clicked.
                    for linked_track, linked_item, _ in linked_items(group):
                        linked_track["clips"] = [
                            entry for entry in linked_track.get("clips", [])
                            if entry is not linked_item
                        ]
                else:
                    del track["clips"][index]
            else:
                cue_track, cue, cue_index = _find_timeline_cue(working, item_id)
                if cue is None or cue_track is None:
                    return fail("timeline item not found")
                del cue_track["cues"][cue_index]
        elif op_name in {"close_gap", "close_gap_before"}:
            track, item, _ = _find_timeline_item(working, item_id)
            if item is None or track is None:
                return fail("timeline item not found")
            current_start = max(0.0, _number(item.get("start"), 0.0))
            previous_end = 0.0
            for candidate in track.get("clips", []) or []:
                if not isinstance(candidate, dict) or candidate.get("id") == item_id:
                    continue
                candidate_start = max(0.0, _number(candidate.get("start"), 0.0))
                candidate_end = candidate_start + max(0.0, _number(candidate.get("duration"), 0.0))
                # The nearest preceding item may overlap the selected item.
                # Include its full end here so an overlap is treated as no
                # gap instead of incorrectly collapsing the selected item to
                # time zero.
                if candidate_start <= current_start + 0.0001:
                    previous_end = max(previous_end, candidate_end)
            gap = max(0.0, current_start - previous_end)
            if gap > 0.0001:
                for candidate in track.get("clips", []) or []:
                    if not isinstance(candidate, dict) or candidate.get("id") == item_id:
                        continue
                    candidate_start = max(0.0, _number(candidate.get("start"), 0.0))
                    if candidate_start >= current_start - 0.0001:
                        candidate["start"] = round(max(0.0, candidate_start - gap), 6)
                item["start"] = round(max(0.0, current_start - gap), 6)
        elif op_name in {"ripple_delete", "ripple_remove"}:
            track, item, index = _find_timeline_item(working, item_id)
            if item is None or track is None:
                return fail("timeline item not found")
            group = linked_group_for(item)
            targets = linked_items(group) if group else [(track, item, index)]
            # Ripple each affected lane independently.  A/V members can have
            # different source durations; each lane therefore closes by the
            # duration of its own removed member.
            for target_track, target_item, _ in list(targets):
                removed_start = max(0.0, _number(target_item.get("start"), 0.0))
                removed_end = removed_start + max(0.0, _number(target_item.get("duration"), 0.0))
                removed_duration = max(0.001, removed_end - removed_start)
                target_track["clips"] = [
                    entry for entry in target_track.get("clips", [])
                    if entry is not target_item
                ]
                for candidate in target_track.get("clips", []) or []:
                    if not isinstance(candidate, dict):
                        continue
                    candidate_start = max(0.0, _number(candidate.get("start"), 0.0))
                    if candidate_start >= removed_end - 0.0001:
                        candidate["start"] = round(max(0.0, candidate_start - removed_duration), 6)
        elif op_name == "move":
            track, item, index = _find_timeline_item(working, item_id)
            if item is None or track is None:
                cue_track, cue, cue_index = _find_timeline_cue(working, item_id)
                if cue is None or cue_track is None:
                    return fail("timeline item not found")
                if str(raw.get("track_id") or "text-main") != "text-main":
                    return fail("invalid destination track")
                new_start = _number(raw.get("start"), cue.get("start", 0.0))
                if new_start < 0 or new_start > 24 * 60 * 60:
                    return fail("invalid timeline start")
                length = max(0.001, _number(cue.get("end"), 0.0) - _number(cue.get("start"), 0.0))
                cue["start"] = round(new_start, 6)
                cue["end"] = round(min(24 * 60 * 60, new_start + length), 6)
                if cue["end"] <= cue["start"]:
                    return fail("invalid subtitle range")
                valid, error = validate_timeline(document, working)
                if not valid:
                    return fail(error or "invalid timeline")
                history.append(before)
                if len(history) > TIMELINE_MAX_HISTORY:
                    history.pop(0)
                redo.clear()
                continue
            new_start = _number(raw.get("start"), item.get("start", 0.0))
            if new_start < 0 or new_start > 24 * 60 * 60:
                return fail("invalid timeline start")
            destination = str(raw.get("track_id") or track.get("id") or "")
            if destination not in TIMELINE_TRACKS or TIMELINE_TRACKS[destination] != track.get("kind"):
                return fail("invalid destination track")
            if destination != str(track.get("id") or ""):
                _apply_lane_transform_default(item, destination)
            original_start = _number(item.get("start"), 0.0)
            item["start"] = round(new_start, 6)
            link_group = linked_group_for(item)
            if link_group:
                delta = new_start - original_start
                for linked_track in working.get("tracks", []):
                    for linked in (linked_track.get("clips", []) if isinstance(linked_track, dict) else []):
                        if linked is item or str(linked.get("link_group") or "") != link_group:
                            continue
                        linked["start"] = round(max(0.0, min(24 * 60 * 60, _number(linked.get("start"), 0.0) + delta)), 6)
            if destination != track.get("id"):
                track["clips"].pop(index)
                _timeline_item_list(working, destination).append(item)
        elif op_name == "trim":
            track, item, index = _find_timeline_item(working, item_id)
            if item is None or track is None:
                return fail("timeline item not found")
            if linked_group_for(item):
                return fail("unlink the A/V group before trimming a linked item")
            in_point = _number(raw.get("in"), item.get("in", 0.0))
            out_point = _number(raw.get("out"), item.get("out", 0.0))
            source_duration = _timeline_source_duration(document, item)
            if in_point < 0 or out_point <= in_point or out_point > source_duration + 0.001:
                return fail("invalid trim range")
            item["in"], item["out"] = in_point, out_point
            updated = _replace_timeline_item(document, item, str(track.get("id")), index)
            if updated is None:
                return fail("invalid trim range")
            track["clips"][index] = updated
        elif op_name == "split":
            track, item, index = _find_timeline_item(working, item_id)
            if item is None or track is None:
                cue_track, cue, cue_index = _find_timeline_cue(working, item_id)
                if cue is None or cue_track is None:
                    return fail("timeline item not found")
                at = _number(raw.get("at"), -1.0)
                cue_start = _number(cue.get("start"), 0.0)
                cue_end = _number(cue.get("end"), cue_start + 1.0)
                if at <= cue_start or at >= cue_end:
                    return fail("invalid split position")
                left = copy.deepcopy(cue)
                right = copy.deepcopy(cue)
                left["id"] = stable_id("cue", cue.get("id"), "left", round(at, 6))
                right["id"] = stable_id("cue", cue.get("id"), "right", round(at, 6))
                left["end"] = round(at, 6)
                right["start"] = round(at, 6)
                cue_track["cues"][cue_index:cue_index + 1] = [left, right]
                valid, error = validate_timeline(document, working)
                if not valid:
                    return fail(error or "invalid timeline")
                history.append(before)
                if len(history) > TIMELINE_MAX_HISTORY:
                    history.pop(0)
                redo.clear()
                continue
            at = _number(raw.get("at"), -1.0)
            if at <= 0 or at >= _number(item.get("duration"), 0.0):
                return fail("invalid split position")
            if linked_group_for(item):
                return fail("unlink the A/V group before splitting a linked item")
            speed = max(0.25, _number(item.get("speed"), 1.0))
            split_source = _number(item.get("in"), 0.0) + at * speed
            first = copy.deepcopy(item)
            second = copy.deepcopy(item)
            first["out"] = split_source
            second["in"] = split_source
            first["id"] = item["id"]
            second["id"] = stable_id("tl", item["id"], "split", round(split_source, 6))
            first_n = _replace_timeline_item(document, first, str(track.get("id")), index)
            second_n = _replace_timeline_item(document, second, str(track.get("id")), index + 1)
            if first_n is None or second_n is None:
                return fail("unable to split timeline item")
            second_n["start"] = round(_number(item.get("start"), 0.0) + at, 6)
            track["clips"][index:index + 1] = [first_n, second_n]
        elif op_name == "duplicate":
            track, item, index = _find_timeline_item(working, item_id)
            if item is None or track is None:
                return fail("timeline item not found")
            if len(track.get("clips", [])) >= TIMELINE_MAX_ITEMS:
                return fail("timeline item limit exceeded")
            group = linked_group_for(item)
            if group:
                # Duplicate the whole A/V unit and mint a fresh group ID so
                # the duplicate does not remain coupled to its source.
                members = linked_items(group)
                if len(members) > TIMELINE_MAX_ITEMS:
                    return fail("timeline item limit exceeded")
                new_group = stable_id("link", group, "duplicate", len(track.get("clips", [])))
                for member_track, member, _ in members:
                    if len(member_track.get("clips", [])) >= TIMELINE_MAX_ITEMS:
                        return fail("timeline item limit exceeded")
                    duplicate_member = copy.deepcopy(member)
                    duplicate_member["id"] = stable_id("tl", member.get("id"), "duplicate", len(member_track.get("clips", [])))
                    duplicate_member["start"] = round(_number(member.get("start"), 0.0) + _number(item.get("duration"), 0.0), 6)
                    duplicate_member["link_group"] = new_group
                    member_track["clips"].append(duplicate_member)
            else:
                duplicate = copy.deepcopy(item)
                duplicate["id"] = stable_id("tl", item_id, "duplicate", len(track["clips"]))
                duplicate["start"] = round(_number(item.get("start"), 0.0) + _number(item.get("duration"), 0.0), 6)
                duplicate["link_group"] = None
                track["clips"].insert(index + 1, duplicate)
        elif op_name == "set_speed":
            track, item, index = _find_timeline_item(working, item_id)
            if item is None or track is None:
                return fail("timeline item not found")
            speed = _number(raw.get("speed"), 1.0)
            if speed < 0.25 or speed > 4.0:
                return fail("invalid speed")
            item["speed"] = speed
            updated = _replace_timeline_item(document, item, str(track.get("id")), index)
            if updated is None:
                return fail("invalid speed")
            track["clips"][index] = updated
        elif op_name == "set_transform":
            track, item, _ = _find_timeline_item(working, item_id)
            if item is None or track is None or track.get("kind") != "video":
                return fail("timeline item not found")
            transform = raw.get("transform")
            if not isinstance(transform, dict):
                return fail("invalid transform")
            item["transform"] = {
                "x": max(-10000.0, min(10000.0, _number(transform.get("x"), 0.0))),
                "y": max(-10000.0, min(10000.0, _number(transform.get("y"), 0.0))),
                "scale": max(0.05, min(20.0, _number(transform.get("scale"), 1.0))),
                "rotate": max(-3600.0, min(3600.0, _number(transform.get("rotate"), 0.0))),
                "opacity": max(0.05, min(1.0, _number(transform.get("opacity"), 1.0))),
                "border": max(0.0, min(12.0, _number(transform.get("border"), 0.0))),
            }
        elif op_name in {"set_link_group", "link_av"}:
            group = str(raw.get("link_group") or "").strip()
            if group and not ID_RE.fullmatch(group):
                return fail("invalid link group")
            item_ids = raw.get("item_ids") if isinstance(raw.get("item_ids"), list) else [item_id]
            if len(item_ids) > 12:
                return fail("too many linked items")
            selected: list[tuple[dict, dict]] = []
            for candidate_id in item_ids:
                track, item, _ = _find_timeline_item(working, str(candidate_id))
                if item is None or track is None or track.get("kind") not in {"video", "audio"}:
                    return fail("timeline item not found")
                selected.append((track, item))
            if not selected:
                return fail("timeline item not found")
            if group:
                kinds = {str(track.get("kind")) for track, _ in selected}
                if not {"video", "audio"}.issubset(kinds):
                    return fail("a link group requires video and audio items")
                # Every selected member is assigned to exactly this group;
                # callers provide the complete group when merging existing
                # groups.  This avoids silently leaving stale members behind.
                for _, item in selected:
                    item["link_group"] = group
            else:
                for _, item in selected:
                    item["link_group"] = None
        elif op_name == "set_canvas":
            canvas = _timeline_canvas(raw.get("canvas"), working.get("canvas", {}))
            if canvas is None:
                return fail("invalid canvas")
            working["canvas"] = canvas
        elif op_name in {"set_track", "set_track_state"}:
            target_track = str(raw.get("track_id") or raw.get("track") or "").strip()
            track = tracks.get(target_track)
            if not isinstance(track, dict) or target_track not in TIMELINE_TRACKS:
                return fail("invalid timeline track")
            if "muted" in raw and not isinstance(raw.get("muted"), bool):
                return fail("invalid track state")
            if "hidden" in raw and not isinstance(raw.get("hidden"), bool):
                return fail("invalid track state")
            if "muted" in raw:
                track["muted"] = bool(raw.get("muted"))
            if "hidden" in raw:
                track["hidden"] = bool(raw.get("hidden"))
        elif op_name == "set_transition":
            track, item, _ = _find_timeline_item(working, item_id)
            if item is None or track is None:
                return fail("timeline item not found")
            which = str(raw.get("which") or "out").lower()
            if which not in {"in", "out"}:
                return fail("invalid transition edge")
            kind = str(raw.get("kind") or "cut").lower()
            if kind not in TIMELINE_TRANSITIONS:
                return fail("invalid transition")
            duration = max(0.0, min(2.0, _number(raw.get("duration"), 0.3 if kind != "cut" else 0.0)))
            easing = str(raw.get("easing") or "linear").lower()
            if easing not in TIMELINE_EASINGS:
                return fail("invalid transition")
            transition_value = {"kind": kind, "duration": duration}
            if easing != "linear":
                transition_value["easing"] = easing
            item[f"transition_{which}"] = transition_value
            if kind in {"fade", "dip_black", "dip_white"}:
                # Resolve adjacency and duration immediately, so a short clip
                # or a stale inspector value cannot produce a 400 on commit.
                _clamp_timeline_transitions(working)
        elif op_name == "set_audio":
            if item_id:
                track, item, _ = _find_timeline_item(working, item_id)
                if item is None or track is None or track.get("kind") not in {"video", "audio"}:
                    return fail("timeline item not found")
                item["gain"] = max(-60.0, min(24.0, _number(raw.get("gain"), item.get("gain", 0.0))))
                item["fade_in"] = max(0.0, min(30.0, _number(raw.get("fade_in"), item.get("fade_in", 0.0))))
                item["fade_out"] = max(0.0, min(30.0, _number(raw.get("fade_out"), item.get("fade_out", 0.0))))
                item["detach"] = bool(raw.get("detach", item.get("detach", False)))
            else:
                audio = working.setdefault("audio", {})
                if not isinstance(audio, dict):
                    audio = {}
                    working["audio"] = audio
                audio["bgm_gain"] = max(-60.0, min(24.0, _number(raw.get("gain"), audio.get("bgm_gain", 0.0))))
                audio["mute_original"] = bool(raw.get("detach", raw.get("mute_original", audio.get("mute_original", False))))
        elif op_name == "set_caption":
            cue = raw.get("cue") if isinstance(raw.get("cue"), dict) else raw
            text_value = str(cue.get("text") or "").strip()[:2000]
            if not text_value or _looks_private(text_value):
                return fail("invalid caption")
            start = max(0.0, _number(cue.get("start"), 0.0))
            end = max(start + 0.001, _number(cue.get("end"), start + 1.0))
            if end > 24 * 60 * 60:
                return fail("invalid caption range")
            text_track = tracks.get("text-main")
            if text_track is None:
                return fail("subtitle track unavailable")
            cue_id = str(cue.get("id") or "").strip()
            if cue_id and not ID_RE.fullmatch(cue_id):
                return fail("invalid caption id")
            if not cue_id:
                cue_id = stable_id("cue", start, end, text_value)
            cue_kind = str(cue.get("kind") or "subtitle").strip().lower()
            if cue_kind not in {"subtitle", "title"}:
                cue_kind = "subtitle"
            record = {"id": cue_id, "start": round(start, 6), "end": round(end, 6),
                      "text": text_value, "kind": cue_kind,
                      "style": _caption_style(cue.get("style"))}
            cues = text_track.setdefault("cues", [])
            existing = next((idx for idx, value in enumerate(cues) if value.get("id") == cue_id), None)
            if existing is None:
                if len(cues) >= TIMELINE_MAX_CUES:
                    return fail("caption limit exceeded")
                cues.append(record)
            else:
                cues[existing] = record
        elif op_name == "shift_track":
            if str(raw.get("track_id") or raw.get("track") or "text-main") != "text-main":
                return fail("invalid destination track")
            text_track = tracks.get("text-main")
            if not isinstance(text_track, dict):
                return fail("subtitle track unavailable")
            delta = max(-86400.0, min(86400.0, _number(raw.get("delta"), 0.0)))
            for cue in text_track.get("cues", []):
                if not isinstance(cue, dict):
                    continue
                length = max(0.001, _number(cue.get("end"), 0.0) - _number(cue.get("start"), 0.0))
                new_start = max(0.0, _number(cue.get("start"), 0.0) + delta)
                cue["start"] = round(new_start, 6)
                cue["end"] = round(min(24 * 60 * 60, new_start + length), 6)
        else:
            return fail("unsupported timeline operation")

        # Normalize transition edges after every operation.  A fade requested
        # on a timeline edge or across a gap is intentionally downgraded to a
        # cut; this is the documented editor behavior and avoids making an
        # otherwise valid edit fail merely because a neighboring clip has not
        # been placed yet.
        _clamp_timeline_transitions(working)
        valid, error = validate_timeline(document, working)
        if not valid:
            return fail(error or "invalid timeline")
        history.append(before)
        if len(history) > TIMELINE_MAX_HISTORY:
            history.pop(0)
        redo.clear()

    return working, None


def normalize_project(raw: object, slug: str | None = None, *, hydrate_media: bool = True) -> dict:
    p = copy.deepcopy(raw) if isinstance(raw, dict) else empty_project()
    if slug:
        p["slug"] = slug
    p.setdefault("title", p.get("slug") or "未命名项目")
    p.setdefault("type", "clone")
    p.setdefault("status", "draft")
    p.setdefault("created", now())
    p.setdefault("updated", now())
    p.setdefault("presets", {"ratio": "16:9", "clarity": "Standard"})
    source = p.get("source") if isinstance(p.get("source"), dict) else {}
    for key, default in (("file", None), ("origin_url", None), ("duration", None), ("segments", []), ("analysis", None)):
        source.setdefault(key, default)
    p["source"] = source
    legacy_assets = dedupe(p.get("assets"), "as")
    legacy_clips = dedupe(p.get("clips"), "clip")
    assembly_raw = p.get("assembly") if isinstance(p.get("assembly"), dict) else {}
    canonical = p.get("asset") if isinstance(p.get("asset"), dict) else {}
    scripts, media = dedupe(canonical.get("scripts"), "scr"), dedupe(canonical.get("media"), "as")
    clips, finals = dedupe(canonical.get("clips"), "clip"), dedupe(canonical.get("finals"), "final")
    script_ids, media_ids = {str(x.get("id")) for x in scripts}, {str(x.get("id")) for x in media}
    roles = {"script", "storyboard", "subtitle", "analysis_report"}
    # Build the alias lookup once per collection. Large manifests used to
    # normalize each existing path again for every legacy record (quadratic).
    record_lookups: dict[int, tuple[set[str], set[str]]] = {}

    def has_record(entries: list[dict], item: dict) -> bool:
        lookup = record_lookups.get(id(entries))
        if lookup is None:
            lookup = ({str(existing.get("id") or "") for existing in entries},
                      {rel for existing in entries
                       if (rel := norm_rel(existing.get("file") or existing.get("path")))})
            record_lookups[id(entries)] = lookup
        ident = str(item.get("id") or "")
        rel = norm_rel(item.get("file") or item.get("path"))
        if ident in lookup[0] or (rel and rel in lookup[1]):
            return True
        # Every false result below is immediately followed by an append.
        lookup[0].add(ident)
        if rel:
            lookup[1].add(rel)
        return False

    for item in legacy_assets:
        role = str(item.get("role") or item.get("kind") or "").lower()
        rel = str(item.get("file") or "").replace("\\", "/")
        if role in roles or rel.startswith(("assets/scripts/", "assets/reports/", "subtitles/")):
            if not has_record(scripts, item):
                item.setdefault("role", role if role in roles else "script")
                scripts.append(item)
                script_ids.add(str(item.get("id")))
        elif not has_record(media, item):
            media.append(item)
            media_ids.add(str(item.get("id")))
    clip_ids = {str(x.get("id")) for x in clips}
    for item in legacy_clips:
        if not has_record(clips, item):
            clips.append(item)
            clip_ids.add(str(item.get("id")))
    order = list(assembly_raw.get("order") or [])
    final_ids = {str(x.get("id")) for x in finals}
    for kind, rel, ident, name in (
        ("official", assembly_raw.get("official"), "final_official", "Official final"),
        ("preview", assembly_raw.get("preview"), "final_preview", "Assembly preview"),
    ):
        # A schema-2 renderer may already have registered this same public
        # file under a stable content/job ID while keeping ``assembly`` as the
        # legacy projection.  Do not synthesize a second logical final merely
        # because the compatibility alias ID is absent.
        if rel and not has_record(finals, {"id": ident, "file": rel}):
            finals.append({"id": ident, "kind": kind, "file": rel, "name": name, "preset": None,
                           "from": order, "created": p.get("updated") or now(), "status": "active"})
            final_ids.add(ident)
    for item in assembly_raw.get("exports") or []:
        if not isinstance(item, dict) or not item.get("file"):
            continue
        ident = stable_id("final", item.get("file"), item.get("preset"), item.get("created"))
        if not has_record(finals, {"id": ident, "file": item["file"]}):
            finals.append({"id": ident, "kind": "export", "file": item["file"],
                           "name": item.get("preset") or "Export", "preset": item.get("preset"),
                           "from": order, "created": item.get("created") or p.get("updated") or now(),
                           "status": "active"})
            final_ids.add(ident)
    p["schema"] = 2
    p["asset"] = {"scripts": scripts, "media": media, "clips": clips, "finals": finals}
    p["assets"], p["clips"] = media, clips
    assembly = copy.deepcopy(assembly_raw)
    if not isinstance(assembly.get("order"), list):
        assembly["order"] = order
    if not isinstance(assembly.get("exports"), list):
        assembly["exports"] = []
    assembly.setdefault("order", order)
    assembly.setdefault("transition", "cut")
    assembly.setdefault("audio", {"bgm": None, "bgm_gain": 0, "mute_original": False})
    assembly.setdefault("subtitles", {"file": None, "burn": False})
    assembly.setdefault("preview", next((x.get("file") for x in finals if x.get("kind") == "preview"), None))
    assembly.setdefault("official", next((x.get("file") for x in finals if x.get("kind") == "official"), None))
    assembly.setdefault("exports", [])
    known = {str(x.get("file")) for x in assembly["exports"] if isinstance(x, dict)}
    for item in finals:
        if item.get("kind") == "export" and item.get("file") and str(item["file"]) not in known:
            assembly["exports"].append({"file": item["file"], "preset": item.get("preset") or item.get("name") or "export",
                                        "created": item.get("created") or now(), "from": "assembly"})
    p["assembly"] = assembly
    # Older projects may have valid media files but no duration metadata on
    # their clip/version records.  Hydrate that public metadata on read so
    # every consumer (clip list, asset bin, timeline and API clients) sees the
    # real duration instead of falling back to a one-second placeholder.
    if hydrate_media:
        _hydrate_clip_media_durations(p, slug or str(p.get("slug") or ""))
    return p


class WorkspaceRequestError(Exception):
    def __init__(self, status: int, message: str, project: dict | None = None):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message}
        if project is not None:
            self.payload.update({"project": project, "rev": project.get("rev", 0)})


def workspace_project(slug: str, base_rev: object = None, *, check_rev: bool = False) -> dict:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug) or is_reserved_slug(slug):
        raise WorkspaceRequestError(400, "invalid project")
    manifest = safe_file(slug, "project.json")
    if not manifest:
        raise WorkspaceRequestError(404, "project unavailable")
    raw = read_json(manifest)
    if not isinstance(raw, dict):
        raise WorkspaceRequestError(409, "project requires repair")
    document = normalize_project(raw, slug)
    valid, error = validate_project(document, slug)
    if not valid or document.get("status") == "archived":
        raise WorkspaceRequestError(409, error or "project is archived")
    if check_rev:
        revision = Handler._revision(base_rev)
        if revision is None:
            raise WorkspaceRequestError(400, "bad revision")
        if revision != document.get("rev"):
            raise WorkspaceRequestError(409, "rev conflict", document)
    return document


def workspace_media_path(slug: str, rel: object) -> str | None:
    value = norm_rel(rel)
    if not value or Handler._private_static_path("/" + slug + "/" + value):
        return None
    if os.path.splitext(value)[1].lower() not in IMG_EXT | VID_EXT | AUD_EXT:
        return None
    return safe_file(slug, value)


def workspace_library_entries(document: dict) -> list[dict]:
    slug = str(document.get("slug") or "")
    entries: list[dict] = []
    library_media_ids = {
        str(record.get("id") or "")
        for record in document.get("asset", {}).get("media", [])
        if record.get("status") not in {"trashed", "superseded", "archived"}
    }
    for source_type, collection in (("media", "media"), ("clip", "clips"), ("final", "finals")):
        for record in document.get("asset", {}).get(collection, []):
            if record.get("status") in {"trashed", "superseded", "archived"}:
                continue
            # The library holds reusable sources (media + clips).  A preview or
            # export render is a result of the timeline, not an asset: every
            # preview used to add a second "Studio preview" entry here.
            if source_type == "final":
                if str(record.get("kind") or "").lower() in {"preview"}:
                    continue
                if str(record.get("file") or "").startswith(RENDER_ARTIFACT_PREFIXES):
                    continue
            # A clip prepared from an uploaded asset is already that asset.
            if source_type == "clip" and record.get("media_id") and str(record["media_id"]) in library_media_ids:
                continue
            version = _version_for_clip(record, record.get("current")) if source_type == "clip" else None
            rel = version.get("file") if version else record.get("file")
            source = workspace_media_path(slug, rel)
            if not source:
                continue
            extension = os.path.splitext(source)[1].lower()
            kind = "image" if extension in IMG_EXT else "audio" if extension in AUD_EXT else "video"
            ident = str(record.get("id") or "")
            library_id = ident if source_type == "media" else source_type + ":" + ident
            entry = {"id": library_id, "asset_id": library_id, "record_id": ident,
                     "source_type": source_type, "project_slug": slug,
                     "project_title": document.get("title") or slug, "kind": kind,
                     "file": rel, "name": record.get("name") or record.get("title") or ident,
                     "origin": ("export" if source_type == "final" else (record.get("origin") or ("generated" if source_type == "clip" else "derived"))),
                     "status": "active", "tags": record.get("tags") or [],
                     "group": record.get("group"), "created": record.get("created") or document.get("created"),
                     "duration": (version or {}).get("duration") or record.get("duration")}
            if workspace_media_path(slug, record.get("poster")):
                entry["poster"] = record["poster"]
            elif source_type == "final":
                # Older export records carry no poster.  Derive one from the file
                # itself so the home Exports card can show a real thumbnail, and
                # read the duration the same way the editor does.
                poster_name = ident + ".jpg"
                poster_path = os.path.join(project_dir(slug), "media", "posters", poster_name)
                try:
                    if not os.path.isfile(poster_path):
                        os.makedirs(os.path.dirname(poster_path), exist_ok=True)
                        if safe_write_target(project_dir(slug), poster_path):
                            make_poster(source, poster_path)
                    if os.path.isfile(poster_path):
                        entry["poster"] = "media/posters/" + poster_name
                except OSError:
                    pass
                if not entry.get("duration"):
                    probed = _cached_media_duration(source)
                    if probed > 0:
                        entry["duration"] = round(probed, 3)
            elif kind == "image":
                entry["poster"] = rel
            if source_type == "clip":
                entry.update({"clip_id": ident, "version": version.get("v") if version else None})
            if isinstance(record.get("gen"), dict):
                entry["gen"] = copy.deepcopy(record["gen"])
            safe_entry = sanitize_public(entry)
            if isinstance(safe_entry, dict):
                entries.append(safe_entry)
    return entries


def workspace_library_usage(documents: list[dict], entries: list[dict]) -> None:
    parents: dict[tuple[str, str], tuple[str, str]] = {}

    def root(identity: tuple[str, str]) -> tuple[str, str]:
        parents.setdefault(identity, identity)
        while parents[identity] != identity:
            parents[identity] = parents[parents[identity]]
            identity = parents[identity]
        return identity

    def connect(identity: tuple[str, str], source: tuple[str, str]) -> None:
        parents[root(identity)] = root(source)

    visible = {(entry["project_slug"], entry["id"]) for entry in entries}
    for document in documents:
        slug = str(document.get("slug") or "")
        assets = document.get("asset") or {}
        for record in assets.get("media", []):
            identity = (slug, str(record.get("id") or ""))
            if identity not in visible:
                continue
            source = record.get("imported_from")
            if (isinstance(source, dict) and isinstance(source.get("project"), str) and source["project"]
                    and isinstance(source.get("asset"), str) and source["asset"]):
                connect(identity, (source["project"], source["asset"]))
        for record in assets.get("clips", []):
            identity = (slug, "clip:" + str(record.get("id") or ""))
            media_identity = (slug, str(record.get("media_id") or ""))
            if identity in visible and media_identity in visible:
                connect(identity, media_identity)

    usage: dict[tuple[str, str], set[str]] = {}
    for document in documents:
        slug = str(document.get("slug") or "")
        assembly = document.get("assembly") or {}
        timeline = assembly.get("timeline")
        tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
        canonical_main = False
        identities: set[tuple[str, str]] = set()
        if isinstance(tracks, list):
            for track in tracks:
                if (not isinstance(track, dict) or not isinstance(track.get("id"), str)
                        or TIMELINE_TRACKS.get(track["id"]) != track.get("kind")):
                    continue
                canonical_main = canonical_main or track.get("id") == "video-main"
                if track.get("kind") not in {"video", "audio"}:
                    continue
                items = track.get("clips")
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("clip_id"):
                        identities.add((slug, "clip:" + str(item["clip_id"])))
                    elif item.get("media_id"):
                        identities.add((slug, str(item["media_id"])))
        if not canonical_main:
            order = assembly.get("order")
            if isinstance(order, list):
                identities.update((slug, "clip:" + clip_id) for clip_id in order if isinstance(clip_id, str))
        for identity in identities & visible:
            usage.setdefault(root(identity), set()).add(slug)

    for entry in entries:
        entry["used_in_count"] = len(usage.get(root((entry["project_slug"], entry["id"])), set()))


def _library_signature(rows: list[dict]) -> tuple | None:
    """Change signature for the library, or None when freshness is unprovable.

    ``None`` disables the cache for this call: a caller that supplies manifests
    from somewhere other than the workspace (tests, imports) must always get a
    freshly built payload instead of a memoized one.
    """

    parts = []
    for row in rows:
        slug = str(row.get("slug") or "")
        if not slug:
            return None
        try:
            stat = os.stat(project_file(slug))
        except OSError:
            return None
        parts.append((slug, int(stat.st_mtime_ns), int(stat.st_size)))
    return (os.path.normcase(os.path.abspath(ROOT)), tuple(parts))


def workspace_library(force: bool = False) -> dict:
    """The reusable-asset library, memoized on the manifests it is built from.

    Building this reads and normalizes every project manifest, walks every
    timeline for usage counts and derives missing export posters, so an uncached
    call dominated page switches (measured 940 ms locally and seconds through a
    remote host).  The signature is cheap: the index token plus each manifest's
    ``(mtime, size)``.  The returned payload is treated as read-only by callers.
    """

    global _LIBRARY_CACHE, _LIBRARY_CACHE_SIG
    index = read_index_cached()
    rows = [row for row in (index.get("projects", []) if isinstance(index, dict) else [])
            if isinstance(row, dict)]
    signature = _library_signature(rows)
    if not force and signature is not None:
        with _LIBRARY_CACHE_LOCK:
            cached, cached_sig = _LIBRARY_CACHE, _LIBRARY_CACHE_SIG
        # The signature decides, with no time limit: a TTL made the next visit
        # after a couple of seconds rebuild everything even when nothing changed
        # (measured 178-265 ms locally and 504 ms through a hosted proxy, on a
        # request the UI makes every time the library is opened).
        if cached is not None and cached_sig == signature:
            return cached
    entries, projects, documents = [], [], []
    for row in rows:
        try:
            document = workspace_project(str(row.get("slug") or ""))
        except WorkspaceRequestError:
            continue
        projects.append(row)
        documents.append(document)
        entries.extend(workspace_library_entries(document))
    workspace_library_usage(documents, entries)
    payload = {"assets": entries, "projects": projects, "index_token": index_change_token(index)}
    if signature is not None:
        with _LIBRARY_CACHE_LOCK:
            _LIBRARY_CACHE = payload
            _LIBRARY_CACHE_SIG = signature
    return payload


def workspace_digest(source: str) -> str:
    digest = hashlib.sha256()
    with open(source, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MEDIA_MAX_BYTES:
            raise WorkspaceRequestError(413, "media is too large")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_copy(source: str, slug: str, rel: str, created: list[str]) -> str:
    target = os.path.join(project_dir(slug), rel.replace("/", os.sep))
    if not safe_write_target(project_dir(slug), target):
        raise WorkspaceRequestError(400, "invalid resource destination")
    if os.path.exists(target):
        if workspace_digest(source) != workspace_digest(target):
            raise WorkspaceRequestError(409, "resource destination already exists")
        return rel
    os.makedirs(os.path.dirname(target), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".media-", dir=os.path.dirname(target))
    try:
        with os.fdopen(descriptor, "wb") as destination, open(source, "rb") as original:
            if os.fstat(original.fileno()).st_size > MEDIA_MAX_BYTES:
                raise WorkspaceRequestError(413, "media is too large")
            shutil.copyfileobj(original, destination, 1024 * 1024)
        if not safe_write_target(project_dir(slug), target) or os.path.lexists(target):
            raise WorkspaceRequestError(409, "resource destination changed")
        os.rename(temporary, target)
        created.append(target)
    finally:
        if os.path.isfile(temporary):
            os.unlink(temporary)
    return rel


def workspace_commit(slug: str, document: dict, op: dict) -> None:
    document["assets"] = document["asset"]["media"]
    document["clips"] = document["asset"]["clips"]
    valid, error = validate_project(document, slug)
    if not valid:
        raise WorkspaceRequestError(400, error or "invalid project")
    state_target = os.path.join(project_dir(slug), "state.json")
    if not safe_write_target(project_dir(slug), state_target):
        raise WorkspaceRequestError(400, "invalid state destination")
    document["rev"] = int(document.get("rev", 0)) + 1
    document["updated"] = now()
    state = read_json(state_target, {})
    state = sanitize_public(state) if isinstance(state, dict) else {}
    state.update({"schema": 1, "rev": document["rev"], "updated": document["updated"]})
    state.setdefault("phase", document.get("status", "draft"))
    write_json_atomic(project_file(slug), document)
    write_json_atomic(state_target, state)
    append_ops(slug, [{**op, "ts": document["updated"]}], document["rev"])
    sync_index()


def workspace_import_asset(slug: str, payload: dict) -> dict:
    source_slug = payload.get("source_project")
    library_id = payload.get("asset_id")
    if not isinstance(source_slug, str) or not isinstance(library_id, str) or len(library_id) > 240:
        raise WorkspaceRequestError(400, "invalid source asset")
    with LOCK:
        target = workspace_project(slug, payload.get("base_rev"), check_rev=True)
        source_project = workspace_project(source_slug)
        entry = next((item for item in workspace_library_entries(source_project) if item["id"] == library_id), None)
    if not entry:
        raise WorkspaceRequestError(404, "registered source asset unavailable")
    if source_slug == slug:
        # Importing from the same project must never copy the file: a media entry
        # reuses its record, and a clip entry already lives in this project.
        if entry["source_type"] == "media":
            record = next(item for item in target["asset"]["media"] if item["id"] == entry["record_id"])
            return {"ok": True, "asset": record, "project": target, "rev": target["rev"], "reused": True}
        if entry["source_type"] == "final":
            # "Add to existing project" on one of the project's own exports: the
            # rendered file is already inside the project, so registering it in the
            # media pool is all that is missing before it can be dragged onto the
            # timeline.  Answering 409 here left every export unusable.
            source = workspace_media_path(source_slug, entry["file"])
            if not source:
                raise WorkspaceRequestError(404, "source media unavailable")
            digest = workspace_digest(source)
            ident = stable_id("as_export", slug, library_id, digest)
            existing = next((item for item in target["asset"]["media"] if item["id"] == ident), None)
            if existing:
                return {"ok": True, "asset": existing, "project": target, "rev": target["rev"], "reused": True}
            record = {"id": ident, "kind": entry["kind"], "file": entry["file"], "name": entry["name"],
                      "origin": "derived", "status": "active", "tags": list(entry.get("tags") or []),
                      "group": entry.get("group"), "used_by": [], "hash": "sha256:" + digest,
                      "created": now(), "imported_from": {"project": source_slug, "asset": library_id}}
            record["duration"] = entry.get("duration") or _probe_media_duration(source)
            poster = entry.get("poster")
            if poster and workspace_media_path(source_slug, poster):
                record["poster"] = poster
            with LOCK:
                target = workspace_project(slug, payload.get("base_rev"), check_rev=True)
                target["asset"]["media"] = [item for item in target["asset"]["media"] if item["id"] != ident] + [record]
                workspace_commit(slug, target, {"op": "asset.import", "id": ident, "source_project": source_slug})
            return {"ok": True, "asset": record, "project": target, "rev": target["rev"], "reused": False}
        raise WorkspaceRequestError(409, "That asset is already in this project")
    source = workspace_media_path(source_slug, entry["file"])
    if not source:
        raise WorkspaceRequestError(404, "source media unavailable")
    digest = workspace_digest(source)
    ident = stable_id("as_import", source_slug, library_id, digest)
    existing = next((item for item in target["asset"]["media"] if item["id"] == ident), None)
    if existing and workspace_media_path(slug, existing.get("file")):
        return {"ok": True, "asset": existing, "project": target, "rev": target["rev"], "reused": True}
    origin = entry.get("origin") if entry.get("origin") in {"upload", "generated", "derived"} else "derived"
    folder = "assets/generated" if origin == "generated" else "assets/uploads"
    rel = folder + "/" + ident + os.path.splitext(source)[1].lower()
    record = {"id": ident, "kind": entry["kind"], "file": rel, "name": entry["name"],
              "origin": origin, "status": "active", "tags": list(entry.get("tags") or []),
              "group": entry.get("group"), "used_by": [], "hash": "sha256:" + digest,
              "created": now(), "imported_from": {"project": source_slug, "asset": library_id}}
    if origin == "generated" and "ai-generated" not in record["tags"]:
        record["tags"].append("ai-generated")
    record["duration"] = entry.get("duration") or _probe_media_duration(source)
    created: list[str] = []
    committed = False
    try:
        workspace_copy(source, slug, rel, created)
        if workspace_digest(safe_file(slug, rel)) != digest:
            raise WorkspaceRequestError(409, "source media changed during import")
        poster_source = workspace_media_path(source_slug, entry.get("poster"))
        if entry["kind"] == "image":
            record["poster"] = rel
        elif poster_source and os.path.splitext(poster_source)[1].lower() in IMG_EXT:
            poster_rel = "media/posters/" + ident + os.path.splitext(poster_source)[1].lower()
            record["poster"] = workspace_copy(poster_source, slug, poster_rel, created)
        generation = entry.get("gen") if isinstance(entry.get("gen"), dict) else {}
        sidecar_source = safe_file(source_slug, generation.get("sidecar"))
        sidecar = {}
        if sidecar_source and os.path.getsize(sidecar_source) <= 1024 * 1024:
            raw_sidecar = read_json(sidecar_source, {})
            if isinstance(raw_sidecar, dict):
                sidecar = sanitize_public({key: raw_sidecar[key] for key in ("prompt", "model_label", "params", "batch", "parent", "created") if key in raw_sidecar})
        if origin == "generated":
            sidecar_rel = rel + ".json"
            sidecar_target = os.path.join(project_dir(slug), sidecar_rel)
            if not safe_write_target(project_dir(slug), sidecar_target):
                raise WorkspaceRequestError(400, "invalid sidecar destination")
            sidecar.setdefault("created", record["created"])
            sidecar.setdefault("model_label", entry["kind"])
            if not os.path.exists(sidecar_target):
                write_json_atomic(sidecar_target, sidecar)
                created.append(sidecar_target)
            record["gen"] = {"sidecar": sidecar_rel, "model_label": entry["kind"]}
            if generation.get("prompt_summary") and not _looks_private(generation["prompt_summary"]):
                record["gen"]["prompt_summary"] = generation["prompt_summary"]
        with LOCK:
            target = workspace_project(slug, payload.get("base_rev"), check_rev=True)
            target["asset"]["media"] = [item for item in target["asset"]["media"] if item["id"] != ident] + [record]
            workspace_commit(slug, target, {"op": "asset.import", "id": ident, "source_project": source_slug})
            committed = True
        return {"ok": True, "asset": record, "project": target, "rev": target["rev"], "reused": False}
    finally:
        if not committed:
            for filename in reversed(created):
                if os.path.isfile(filename) and not os.path.islink(filename):
                    os.unlink(filename)


def workspace_prepare_media(slug: str, payload: dict) -> dict:
    media_id = payload.get("media_id")
    with LOCK:
        document = workspace_project(slug, payload.get("base_rev"), check_rev=True)
        media = next((item for item in document["asset"]["media"] if item.get("id") == media_id and item.get("status") == "active"), None)
    if not media or media.get("kind") not in {"image", "video"}:
        raise WorkspaceRequestError(400, "select an image or video asset")
    source = workspace_media_path(slug, media.get("file"))
    if not source:
        raise WorkspaceRequestError(404, "media file unavailable")
    duration = 0.0
    image_source = media["kind"] == "image"
    if image_source:
        raw_duration = payload.get("duration", 4)
        duration = _number(raw_duration, -1)
        if isinstance(raw_duration, bool) or not 0.1 <= duration <= 60:
            raise WorkspaceRequestError(400, "image duration must be between 0.1 and 60 seconds")
        duration = round(duration, 3)
    digest = workspace_digest(source)
    clip_id = stable_id("clip_media", media_id, digest, duration if image_source else "video")
    existing = next((item for item in document["asset"]["clips"] if item["id"] == clip_id), None)
    if existing and workspace_media_path(slug, (_version_for_clip(existing, existing.get("current")) or {}).get("file")):
        return {"ok": True, "clip": existing, "asset": media, "project": document, "rev": document["rev"], "reused": True}
    if not image_source:
        duration = _probe_media_duration(source)
        if duration <= 0:
            raise WorkspaceRequestError(422, "video duration unavailable; check the file and ffprobe")
    elif not has_ffmpeg():
        raise WorkspaceRequestError(501, "ffmpeg unavailable")
    extension = ".mp4" if image_source else os.path.splitext(source)[1].lower()
    rel = "clips/" + clip_id + "/v1" + extension
    output = os.path.join(project_dir(slug), rel.replace("/", os.sep))
    if not safe_write_target(project_dir(slug), output):
        raise WorkspaceRequestError(400, "invalid clip destination")
    created: list[str] = []
    committed = False
    temporary = None
    try:
        if image_source:
            if os.path.exists(output):
                raise WorkspaceRequestError(409, "clip destination already exists")
            os.makedirs(os.path.dirname(output), exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".prepare-", suffix=".mp4", dir=os.path.dirname(output))
            os.close(descriptor)
            result = ffmpeg_run(["-threads", "2", "-loop", "1", "-i", source, "-t", str(duration),
                                 "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                                 "-an", "-r", "30", "-c:v", "libx264", "-threads", "2", "-preset", "veryfast",
                                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", temporary], timeout=120)
            if result.returncode != 0 or not os.path.isfile(temporary) or os.path.getsize(temporary) < 100:
                raise WorkspaceRequestError(422, "image could not be prepared for the timeline")
            if not safe_write_target(project_dir(slug), output) or os.path.lexists(output):
                raise WorkspaceRequestError(409, "clip destination changed")
            os.rename(temporary, output)
            created.append(output)
            duration = _probe_media_duration(output) or duration
        else:
            workspace_copy(source, slug, rel, created)
        clip = {"id": clip_id, "title": media.get("name") or "Media clip", "duration": duration,
                "ratio": document.get("presets", {}).get("ratio", "16:9"), "clarity": "Standard",
                "status": "delivered", "outcome": "complete", "current": 1,
                "origin": "derived" if image_source else media.get("origin", "upload"),
                "media_id": media_id, "mappings": [media_id], "created": now(),
                "versions": [{"v": 1, "file": rel, "duration": duration, "created": now(),
                              "note": "Prepared from project media", "superseded": False}]}
        if image_source:
            clip["poster"] = media["file"]
        elif workspace_media_path(slug, media.get("poster")):
            clip["poster"] = media["poster"]
        with LOCK:
            document = workspace_project(slug, payload.get("base_rev"), check_rev=True)
            current_media = next(item for item in document["asset"]["media"] if item["id"] == media_id)
            current_media["used_by"] = list(dict.fromkeys([*(current_media.get("used_by") or []), clip_id]))
            current_media["prepared_clip"] = clip_id
            document["asset"]["clips"] = [item for item in document["asset"]["clips"] if item["id"] != clip_id] + [clip]
            workspace_commit(slug, document, {"op": "asset.prepare", "id": media_id, "clip": clip_id, "duration": duration})
            committed = True
        return {"ok": True, "clip": clip, "asset": current_media, "project": document, "rev": document["rev"], "reused": False}
    finally:
        if temporary and os.path.isfile(temporary):
            os.unlink(temporary)
        if not committed:
            for filename in reversed(created):
                if os.path.isfile(filename) and not os.path.islink(filename):
                    os.unlink(filename)


def project_dir(slug: str) -> str:
    return os.path.join(ROOT, slug)


def project_file(slug: str) -> str:
    return os.path.join(project_dir(slug), "project.json")


def safe_file(slug: str, rel: object, must_exist: bool = True) -> str | None:
    value = norm_rel(rel)
    if not value or not SLUG_RE.match(slug) or is_reserved_slug(slug):
        return None
    project_path = os.path.abspath(project_dir(slug))
    # A project directory must be a real child of ROOT.  Without this check a
    # symlink/junction named after a valid slug could make API reads and ffmpeg
    # operations reach outside the manager workspace.
    if os.path.islink(project_path) or not os.path.isdir(project_path):
        return None
    base = os.path.realpath(project_path)
    if not path_within(ROOT, base):
        return None
    target = os.path.realpath(os.path.join(base, value))
    if not path_within(base, target):
        return None
    if path_contains_link(base, os.path.join(base, value)):
        return None
    if must_exist and not os.path.isfile(target):
        return None
    return target


def validate_project(doc: dict, slug: str) -> tuple[bool, str | None]:
    if not SLUG_RE.match(str(doc.get("slug") or "")) or doc.get("slug") != slug:
        return False, "invalid slug"
    if doc.get("status") not in {"draft", "planning", "generating", "reviewing", "done", "archived"}:
        return False, "invalid status"
    asset = doc.get("asset")
    if not isinstance(asset, dict) or any(not isinstance(asset.get(k), list) for k in ("scripts", "media", "clips", "finals")):
        return False, "invalid asset groups"
    if doc.get("assets") != asset["media"] or doc.get("clips") != asset["clips"]:
        return False, "legacy projection out of sync"
    for group in ("scripts", "media", "clips", "finals"):
        for item in asset[group]:
            if not isinstance(item, dict) or not ID_RE.fullmatch(str(item.get("id") or "")):
                return False, f"invalid id in asset.{group}"
    path_keys = {"file", "path", "poster", "proxy", "filmstrip", "sidecar", "analysis", "preview", "official"}

    def walk(value: object) -> tuple[bool, str | None]:
        if isinstance(value, dict):
            for key, child in value.items():
                if PRIVATE_RE.search(str(key)):
                    return False, "private field is not allowed"
                if isinstance(child, str) and (key in path_keys or key.endswith("_file")):
                    if child and norm_rel(child) is None:
                        return False, f"invalid path in {key}"
                ok, err = walk(child)
                if not ok:
                    return ok, err
        elif isinstance(value, list):
            for child in value:
                ok, err = walk(child)
                if not ok:
                    return ok, err
        elif isinstance(value, str) and (PRIVATE_RE.search(value) or ABSOLUTE_RE.search(value)):
            return False, "private data is not allowed"
        return True, None

    return walk(doc)


def append_ops(slug: str, ops: object, rev: int) -> None:
    if not isinstance(ops, list):
        return
    path = os.path.join(project_dir(slug), "logs", "edits.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for raw in ops[:100]:
            if not isinstance(raw, dict):
                continue
            operation = sanitize_log_entry(raw)
            if not operation.get("op"):
                continue
            operation["rev"] = rev
            operation.setdefault("ts", now())
            handle.write(json.dumps(operation, ensure_ascii=False) + "\n")


def _file_stat_signature(path: str) -> tuple[int, int] | None:
    """Return a small, non-content signature for a regular path.

    ``lstat``/``follow_symlinks=False`` is intentional: the signature helper
    is used on untrusted workspace paths and must never follow a link while
    deciding whether a refresh is needed.
    """

    try:
        try:
            stat_result = os.stat(path, follow_symlinks=False)
        except TypeError:  # pragma: no cover - old Python compatibility
            stat_result = os.lstat(path)
        if os.path.islink(path):
            return None
        return int(stat_result.st_mtime_ns), int(stat_result.st_size)
    except (OSError, ValueError, AttributeError):
        return None


def _project_index_signature() -> tuple[tuple[str, int, int], ...]:
    """Probe direct project metadata without walking project contents.

    The index projection is rebuilt only when one of these bounded entries
    changes.  Normal recorder writes call ``sync_index`` directly, while this
    probe covers a project created/updated by another process.
    """

    rows: list[tuple[str, int, int]] = []
    try:
        with os.scandir(ROOT) as entries:
            names = sorted((entry.name for entry in entries), key=str.casefold)
    except (OSError, ValueError):
        return tuple()
    for name in names[:10000]:
        if is_reserved_slug(name) or not SLUG_RE.fullmatch(name):
            continue
        entry_path = os.path.abspath(os.path.join(ROOT, name))
        try:
            if (os.path.islink(entry_path) or not os.path.isdir(entry_path)
                    or not path_within(ROOT, entry_path)):
                continue
        except OSError:
            continue
        directory_sig = _file_stat_signature(entry_path)
        manifest_sig = _file_stat_signature(os.path.join(entry_path, "project.json"))
        # Include missing manifests as ``-1`` so a later atomic replacement is
        # visible to the probe without opening arbitrary files.
        rows.append((
            name,
            directory_sig[0] if directory_sig else -1,
            manifest_sig[0] if manifest_sig else -1,
        ))
    return tuple(rows)


def _cache_index_snapshot(index: object,
                          *,
                          project_sig: tuple[tuple[str, int, int], ...] | None = None,
                          file_sig: tuple[int, int] | None = None) -> None:
    """Publish an immutable-in-practice copy of the current index."""

    global INDEX_CACHE, INDEX_CACHE_PROJECT_SIG, INDEX_CACHE_FILE_SIG, INDEX_CACHE_ROOT, INDEX_CACHE_LAST_PROBE
    if not isinstance(index, dict):
        index = {"schema": 1, "updated": now(), "projects": []}
    if project_sig is None:
        project_sig = _project_index_signature()
    if file_sig is None:
        file_sig = _file_stat_signature(os.path.join(ROOT, "index.json"))
    with INDEX_CACHE_LOCK:
        INDEX_CACHE = copy.deepcopy(index)
        INDEX_CACHE_PROJECT_SIG = project_sig
        INDEX_CACHE_FILE_SIG = file_sig
        INDEX_CACHE_ROOT = os.path.normcase(os.path.abspath(ROOT))
        INDEX_CACHE_LAST_PROBE = time.monotonic()


def read_index_cached(*, force: bool = False) -> dict:
    """Read the index projection, rebuilding it only after a cheap probe.

    ``force`` is used by explicit write/sync paths.  Browser GETs use the
    default and therefore never recursively walk all project files merely to
    render a list.
    """

    global INDEX_CACHE_LAST_PROBE
    index_path = os.path.join(ROOT, "index.json")
    monotonic_now = time.monotonic()
    root_key = os.path.normcase(os.path.abspath(ROOT))
    with INDEX_CACHE_LOCK:
        cached = copy.deepcopy(INDEX_CACHE) if isinstance(INDEX_CACHE, dict) else None
        cached_project_sig = INDEX_CACHE_PROJECT_SIG
        cached_file_sig = INDEX_CACHE_FILE_SIG
        cached_root = INDEX_CACHE_ROOT
        last_probe = INDEX_CACHE_LAST_PROBE
    # Avoid stat/scandir churn when several browser requests arrive together;
    # writers still update the cache immediately through ``sync_index``.
    if (not force and cached is not None and cached_root == root_key
            and monotonic_now - last_probe < INDEX_PROBE_INTERVAL):
        return cached
    project_sig = _project_index_signature()
    file_sig = _file_stat_signature(index_path)
    if (not force and cached is not None and cached_root == root_key
            and project_sig == cached_project_sig and file_sig == cached_file_sig):
        with INDEX_CACHE_LOCK:
            INDEX_CACHE_LAST_PROBE = monotonic_now
        return cached

    # Serialize the occasional rebuild with recorder writes.  Re-check the
    # cache after acquiring the lock because another request may have won the
    # race while this thread was probing.
    with LOCK:
        with INDEX_CACHE_LOCK:
            current = copy.deepcopy(INDEX_CACHE) if isinstance(INDEX_CACHE, dict) else None
            current_project_sig = INDEX_CACHE_PROJECT_SIG
            current_file_sig = INDEX_CACHE_FILE_SIG
            current_root = INDEX_CACHE_ROOT
        if (not force and current is not None and current_root == root_key
                and project_sig == current_project_sig and file_sig == current_file_sig):
            with INDEX_CACHE_LOCK:
                INDEX_CACHE_LAST_PROBE = monotonic_now
            return current
        sync_index()
        refreshed = read_json(index_path, {"schema": 1, "updated": now(), "projects": []})
        if not isinstance(refreshed, dict):
            refreshed = {"schema": 1, "updated": now(), "projects": []}
        # sync_index has already cached the signatures from before its reads;
        # replacing those with newer signatures could hide an external edit.
        return copy.deepcopy(refreshed)


def index_change_token(index: object | None = None) -> str:
    """Return a short token for list-view refresh checks."""

    # Hash only fields rendered by the list view.  ``sync_index`` may rewrite
    # its projection timestamp during a no-op reconciliation; that must not
    # force every browser to download the same project rows again.
    rows: list[tuple[object, ...]] = []
    if isinstance(index, dict) and isinstance(index.get("projects"), list):
        for item in index["projects"][:10000]:
            if not isinstance(item, dict):
                continue
            rows.append(tuple(item.get(key) for key in (
                "slug", "title", "type", "status", "cover", "clips_done",
                "clips_total", "finals_done", "updated", "size_bytes",
            )))
    source = json.dumps({
        "projects": rows,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:24]


def _watch_path_values() -> list[str]:
    """Return bounded handoff/output directories for the local watcher."""

    values: list[str] = []
    if TASK_DIR:
        values.append(TASK_DIR)
    # The manager inbox is always a safe, narrow fallback.  The scanner itself
    # applies the same path policy before reading it.
    values.append(os.path.join(ROOT, "inbox"))
    # Keep this list aligned with vpm_sync's explicit output variables without
    # importing the scanner into the long-lived HTTP process.
    env_names = (
        "VIDEO_GENERATOR_HANDOFF_DIR", "VIDEO_GENERATOR_TASK_DIR",
        "VIDEO_GENERATOR_OUTPUT_DIR", "VIDEO_GENERATOR_OUTPUT_ROOT",
        "VAM_OUTPUT_DIR", "CAPAFY_OUTPUT_DIR", "CAPAFY_AGENT_OUTPUTS_DIR",
    )
    for name in env_names:
        raw = os.environ.get(name)
        if not raw:
            continue
        # Windows uses ``;``; retain a single POSIX path containing a drive
        # letter when this bundle is inspected from a POSIX host.
        pieces = raw.split(os.pathsep)
        if os.pathsep == ":" and re.match(r"^[A-Za-z]:[\\/].*", raw):
            pieces = [raw]
        values.extend(piece.strip().strip('"') for piece in pieces if piece.strip())
    # Mirror vpm_sync's narrow workspace child fallbacks.  Never add the
    # workspace itself: only direct, named output buckets are fingerprinted.
    workspace_values: list[str] = []
    raw_workspace = os.environ.get("CAPAFY_WORKSPACE")
    if raw_workspace:
        pieces = raw_workspace.split(os.pathsep)
        if os.pathsep == ":" and re.match(r"^[A-Za-z]:[\\/].*", raw_workspace):
            pieces = [raw_workspace]
        workspace_values.extend(piece.strip().strip('"') for piece in pieces if piece.strip())
    workspace_values.append(os.path.join(os.path.expanduser("~"), "workspace"))
    child_names = ("agent-outputs", "outputs", "video-generator-outputs")
    for base in workspace_values:
        for child in child_names:
            values.append(os.path.join(base, child))
        values.append(os.path.join(base, ".capafy", "video-generator-outputs"))
    # Include the conventional default inbox only when it is bounded and
    # already present; this catches generators that do not set an env var.
    values.append(os.path.join(tempfile.gettempdir(), "video-generator-handoffs"))
    for child in child_names:
        values.append(os.path.join(tempfile.gettempdir(), child))
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        try:
            absolute = os.path.abspath(os.path.expanduser(value))
            key = os.path.normcase(absolute)
        except (OSError, TypeError, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            unique.append(absolute)
        if len(unique) >= 32:
            break
    return unique


def _bounded_directory_signature(path: str) -> tuple[object, ...]:
    """Fingerprint direct handoff entries, never recursively."""

    absolute = os.path.abspath(path)
    try:
        # ``islink`` does not report every Windows junction/reparse point;
        # reject a realpath change as the conservative equivalent before
        # opening/scanning an environment-provided directory.
        if (os.path.islink(absolute)
                or os.path.normcase(os.path.realpath(absolute)) != os.path.normcase(absolute)):
            return (os.path.normcase(absolute), None)
    except (OSError, RuntimeError, ValueError):
        return (os.path.normcase(absolute), None)
    directory_sig = _file_stat_signature(absolute)
    if directory_sig is None or not os.path.isdir(absolute):
        return (os.path.normcase(absolute), None)
    entries: list[tuple[str, int, int]] = []
    try:
        with os.scandir(absolute) as scanned:
            for entry in scanned:
                if len(entries) >= WATCH_SIGNATURE_LIMIT:
                    break
                if entry.name.startswith("."):
                    continue
                suffix = os.path.splitext(entry.name)[1].lower()
                if suffix not in VID_EXT and suffix != ".json":
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    stat_result = entry.stat(follow_symlinks=False)
                except (OSError, TypeError):
                    continue
                entries.append((entry.name, int(stat_result.st_mtime_ns), int(stat_result.st_size)))
    except (OSError, ValueError):
        return (os.path.normcase(absolute), directory_sig, "error")
    entries.sort(key=lambda item: (item[0].casefold(), item[0]))
    return (os.path.normcase(absolute), directory_sig, tuple(entries), len(entries))


def _watch_signature() -> tuple[object, ...]:
    """Build one bounded signature for handoffs and optional task maps.

    Do not include manager-owned ``.handoff_pending.json`` here: the scanner
    writes it itself, and including it would make a successful scan look like
    a new handoff on the next tick.  Direct project manifest metadata is
    included so a pending mapping becomes eligible soon after preflight creates
    its destination; one cheap reconciliation pass after a scanner write is
    preferable to waiting for the long pending retry interval.
    """

    directories = tuple(_bounded_directory_signature(path) for path in _watch_path_values())
    files = tuple(
        (name, _file_stat_signature(path))
        for name, path in (
            ("map", TASK_MAP),
        )
        if path
    )
    return (directories, files, _project_index_signature())


WATCH_SIGNATURE_LOCK = threading.Lock()
WATCH_LAST_SIGNATURE: tuple[object, ...] | None = None


def _watch_signature_changed(*, force: bool = False) -> bool:
    global WATCH_LAST_SIGNATURE
    signature = _watch_signature()
    # Do not consume a change while a scanner is already running.  A producer
    # can finish writing a second envelope during that scan; leaving the last
    # observed signature untouched lets the next watcher tick schedule the
    # follow-up pass instead of losing that handoff.
    with SYNC_STATE_LOCK:
        scanner_running = bool(SYNC_RUNNING)
    with WATCH_SIGNATURE_LOCK:
        changed = force or WATCH_LAST_SIGNATURE is None or signature != WATCH_LAST_SIGNATURE
        if changed and not scanner_running:
            WATCH_LAST_SIGNATURE = signature
    return changed


def _revision_value(value: object) -> int:
    """Parse a non-negative public revision without silently truncating."""

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and re.fullmatch(r"\d{1,18}", value.strip()):
        try:
            return max(0, int(value.strip()))
        except (TypeError, ValueError, OverflowError):
            return 0
    return 0


def project_change_snapshot(slug: str) -> dict[str, object] | None:
    """Return lightweight project/state metadata for browser change polling."""

    if not SLUG_RE.fullmatch(str(slug or "")) or is_reserved_slug(slug):
        return None
    directory = os.path.abspath(project_dir(slug))
    manifest_path = os.path.abspath(project_file(slug))
    if (os.path.islink(directory) or not os.path.isdir(directory)
            or not path_within(ROOT, directory)
            or os.path.islink(manifest_path) or not os.path.isfile(manifest_path)
            or not path_within(directory, manifest_path)
            or path_contains_link(ROOT, manifest_path)):
        return None
    raw = read_json(manifest_path, None)
    if not isinstance(raw, dict):
        return None
    # Change polling does not need the normalized asset graph.  Avoid a deep
    # copy/dedupe/legacy projection pass here; the full project endpoint keeps
    # the stronger validation boundary when a token actually changes.
    if raw.get("slug") != slug:
        return None
    status_value = str(raw.get("status") or "draft")
    if status_value not in {"draft", "planning", "generating", "reviewing", "done", "archived"}:
        return None
    state_path = os.path.join(directory, "state.json")
    state: dict[str, object] = {}
    if (not os.path.islink(state_path) and os.path.isfile(state_path)
            and path_within(directory, state_path)
            and not path_contains_link(directory, state_path)):
        candidate = read_json(state_path, {})
        if isinstance(candidate, dict):
            state = candidate
    project_stat = _file_stat_signature(manifest_path) or (-1, -1)
    state_stat = _file_stat_signature(state_path) or (-1, -1)
    project_rev = _revision_value(raw.get("rev"))
    state_rev = _revision_value(state.get("rev"))
    project_updated = str(raw.get("updated") or "")[:64]
    state_updated = str(state.get("updated") or "")[:64]
    phase = str(state.get("phase") or status_value or "draft")[:32]
    active_clip = state.get("active_clip")
    if not isinstance(active_clip, (str, int)):
        active_clip = None
    # Include file stat values in addition to logical revisions.  This catches
    # an atomic state/manifest replacement that preserved an old revision and
    # gives the browser a deterministic token without hashing media bytes.
    token_source = json.dumps({
        "project_rev": project_rev,
        "state_rev": state_rev,
        "project_updated": project_updated,
        "state_updated": state_updated,
        "project_stat": project_stat,
        "state_stat": state_stat,
        "status": status_value,
        "phase": phase,
        "active_clip": active_clip,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    token = hashlib.sha256(token_source.encode("utf-8", "replace")).hexdigest()[:24]
    return {
        "slug": slug,
        "token": token,
        "project_rev": project_rev,
        "state_rev": state_rev,
        "project_updated": project_updated,
        "state_updated": state_updated,
        "updated": project_updated or state_updated,
        "status": status_value[:32],
        "phase": phase,
        "active_clip": active_clip,
    }


_PROJECT_SIZE_CACHE: dict[str, tuple[tuple[int, int, int], int]] = {}
_PROJECT_SIZE_CACHE_MAX = 200


def _project_size_cached(slug: str) -> int:
    """Return a project's on-disk byte size without re-walking it every write.

    ``sync_index`` runs after every save/commit/render/scan; walking every
    file of every project (and stat-ing renders/proxies) dominated index
    rebuilds on large projects.  The cache is keyed on the project directory
    mtime plus the manifest stat, which together cover file add/remove and
    every manifest rewrite.  Media files rewritten in place without a
    directory change keep the previous value — acceptable for the list view.
    """

    directory = os.path.abspath(project_dir(slug))
    manifest = os.path.abspath(project_file(slug))
    try:
        stamp = (int(os.stat(directory).st_mtime_ns),
                 int(os.stat(manifest).st_mtime_ns),
                 int(os.stat(manifest).st_size))
    except (OSError, ValueError):
        return 0
    cache_key = os.path.normcase(directory)
    entry = _PROJECT_SIZE_CACHE.get(cache_key)
    if entry is not None and entry[0] == stamp:
        return entry[1]
    if len(_PROJECT_SIZE_CACHE) >= _PROJECT_SIZE_CACHE_MAX:
        _PROJECT_SIZE_CACHE.clear()
    if not path_within(ROOT, directory) or path_contains_link(ROOT, directory):
        return 0
    size = 0
    pending = [directory]
    while pending:
        base = pending.pop()
        try:
            # Recheck each directory before descending in case another writer
            # replaced it with a link after its parent was enumerated.
            if not path_within(directory, base) or path_contains_link(directory, base):
                continue
            with os.scandir(base) as entries:
                for child in entries:
                    if base == directory and child.name == "staging":
                        continue
                    try:
                        info = child.stat(follow_symlinks=False)
                        # Never descend into links/junctions or count their
                        # targets. DirEntry carries this metadata already on
                        # Windows; resolving every file's final path twice was
                        # the dominant cost after each timeline save.
                        if (stat.S_ISLNK(info.st_mode)
                                or getattr(info, "st_file_attributes", 0)
                                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(child.path)
                        else:
                            size += info.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    _PROJECT_SIZE_CACHE[cache_key] = (stamp, size)
    return size


def sync_index(*, timeline_slug: str | None = None) -> None:
    """Publish the index atomically, reusing unaffected rows after an edit.

    Only the timeline endpoint requests an incremental update. A missing cache,
    external index write, or another project's changed signature requires the
    full reconciliation, so new deliveries and external edits remain visible.
    Serialize all publishers with project writes to avoid an older background
    render/index snapshot overwriting a newer editor projection.
    """
    with LOCK:
        project_sig = _project_index_signature()
        rows = None
        if timeline_slug is not None:
            root_key = os.path.normcase(os.path.abspath(ROOT))
            file_sig = _file_stat_signature(os.path.join(ROOT, "index.json"))
            with INDEX_CACHE_LOCK:
                if (INDEX_CACHE_ROOT == root_key and file_sig is not None
                        and file_sig == INDEX_CACHE_FILE_SIG
                        and INDEX_CACHE_PROJECT_SIG is not None
                        and isinstance(INDEX_CACHE, dict)
                        and isinstance(INDEX_CACHE.get("projects"), list)
                        and tuple(row for row in project_sig if row[0] != timeline_slug)
                        == tuple(row for row in INDEX_CACHE_PROJECT_SIG if row[0] != timeline_slug)):
                    rows = copy.deepcopy(INDEX_CACHE["projects"])
                    rows = [row for row in rows if row.get("slug") != timeline_slug]
        _sync_index_rows(project_sig, timeline_slug if rows is not None else None, rows)


def _sync_index_rows(project_sig: tuple, timeline_slug: str | None,
                     rows: list[dict] | None) -> None:
    os.makedirs(ROOT, exist_ok=True)
    rows = [] if rows is None else rows
    entries = [timeline_slug] if timeline_slug is not None else sorted(os.listdir(ROOT))
    for entry in entries:
        if is_reserved_slug(entry) or not SLUG_RE.match(entry):
            continue
        entry_path = os.path.abspath(os.path.join(ROOT, entry))
        if os.path.islink(entry_path) or not os.path.isdir(entry_path) or not path_within(ROOT, entry_path):
            continue
        # A manifest is the trust boundary for a project.  Do not follow a
        # symlink/junction supplied by the workspace, even when its target is
        # readable; otherwise an external JSON file could be projected into
        # the shared index (and later returned by the API).  Atomic writes
        # replace the regular file in place, so rejecting links here does not
        # interfere with normal recorder/server writes.
        manifest = os.path.abspath(project_file(entry))
        if (os.path.islink(manifest) or not os.path.isfile(manifest)
                or not path_within(entry_path, manifest)):
            continue
        raw = read_json(manifest)
        if not isinstance(raw, dict):
            continue
        # List rows need names/status/paths only, never frame-rate or duration
        # probes for every media file in every project.
        doc = normalize_project(raw, entry, hydrate_media=False)
        clips = doc["clips"]
        cover = next((entry + "/" + str(c["poster"]) for c in clips if c.get("poster") and norm_rel(c.get("poster"))), None)
        if cover is None:
            cover = next((entry + "/" + str(f["poster"]) for f in reversed(doc["asset"]["finals"])
                          if isinstance(f, dict) and f.get("poster") and norm_rel(f.get("poster"))), None)
        if cover is None:
            # Older export records carry no poster.  Derive one from the newest
            # final so the card still has a cover, without rewriting manifests.
            for item in [f for f in reversed(doc["asset"]["finals"]) if isinstance(f, dict)][:2]:
                rel = norm_rel(item.get("file"))
                if not rel or item.get("poster"):
                    continue
                source = safe_file(entry, rel)
                if not source:
                    continue
                name = "auto-" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12] + ".jpg"
                target = os.path.join(entry_path, "media", "posters", name)
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if safe_write_target(entry_path, target) and make_poster(source, target):
                        cover = entry + "/media/posters/" + name
                        break
                except OSError:
                    continue
        if cover is None:
            newest = []
            try:
                poster_dir = os.path.join(entry_path, "media", "posters")
                for name in os.listdir(poster_dir):
                    if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        newest.append((os.path.getmtime(os.path.join(poster_dir, name)), name))
            except OSError:
                newest = []
            if newest:
                cover = entry + "/media/posters/" + max(newest)[1]
        if cover is None:
            generated = _ensure_project_cover(entry, entry_path, doc)
            if generated:
                cover = entry + "/" + generated
        size = _project_size_cached(entry)
        rows.append({"slug": entry, "title": public_label(doc.get("title"), entry), "type": doc.get("type"),
                     "status": doc.get("status"), "cover": cover,
                     "clips_done": sum(1 for c in clips if c.get("status") == "delivered"),
                     "clips_total": len(clips),
                     "finals_done": sum(1 for f in doc["asset"]["finals"] if f.get("status", "active") == "active"),
                     "updated": doc.get("updated") or "", "size_bytes": size})
    rows.sort(key=lambda row: row["updated"], reverse=True)
    index = {"schema": 1, "updated": now(), "projects": rows}
    index_path = os.path.join(ROOT, "index.json")
    # Windows can briefly hold the projection open while another request is
    # reading it.  Keep the atomic replace contract, but retry the narrow
    # replace a few times instead of surfacing a transient WinError 5 to the
    # browser (or letting concurrent list refreshes fail noisily).
    for attempt in range(5):
        try:
            write_json_atomic(index_path, index)
            # Use the pre-read signatures. If an external writer changed a
            # manifest during the rebuild, the next read must reconcile it.
            _cache_index_snapshot(index, project_sig=project_sig)
            return
        except PermissionError:
            if attempt >= 4:
                return
            time.sleep(0.03 * (attempt + 1))


def ensure_embedded_webapp() -> None:
    """Make direct ``server.py --root`` launches serve the bundled page too.

    ``ensure.py`` normally installs this bundle first, but keeping the direct
    server entrypoint self-contained makes diagnostics and local smoke tests
    predictable without changing any project data.
    """
    webdir = os.path.join(ROOT, "webapp")
    if os.path.islink(webdir) or not path_within(ROOT, webdir):
        # Never copy the bundled files through a symlink/junction supplied by
        # an untrusted workspace.  The API can still serve project data.
        return
    os.makedirs(webdir, exist_ok=True)
    notices = (
        "inter-OFL.txt", "licenses/README.md", "licenses/inter-OFL.txt",
        "licenses/roboto-OFL.txt", "licenses/playfairdisplay-OFL.txt",
        "licenses/bebasneue-OFL.txt", "licenses/dancingscript-OFL.txt",
        "licenses/tooscut-NOTICE.md", "licenses/tooscut-ELASTIC-LICENSE-2.0.txt",
        "licenses/editor-sources.md", "licenses/openreel-MIT.txt", "licenses/opencut-MIT.txt",
    )
    for name in ("index.html", "studio-skin.css", "inter.ttf", "roboto.ttf", "playfairdisplay.ttf", "bebasneue.ttf", "dancingscript.ttf", "workspace.js", "studio-workspace.js", "studio-panels.js", "studio-filmstrip.js", "server.py", "ensure.py", "vpm_privacy.py", "vpm_context.py", "vpm_sync.py", "handoff_core.py", "handoff.py", "receive.py", "vpm_receive.py", "handoff_impl.py", "vpm_record.py", "vpm_prepare.py") + notices:
        source = os.path.join(BUNDLE_DIR, name)
        if name in {"vpm_privacy.py", "vpm_context.py", "vpm_sync.py", "handoff_core.py", "handoff.py", "receive.py", "vpm_receive.py", "handoff_impl.py", "vpm_record.py", "vpm_prepare.py"} and not os.path.isfile(source):
            source = os.path.join(os.path.dirname(os.path.dirname(BUNDLE_DIR)), "scripts", name)
        destination = os.path.join(webdir, name)
        if os.path.abspath(source) == os.path.abspath(destination):
            continue
        if os.path.isfile(source) and not os.path.exists(destination):
            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(source, destination)
            except OSError:
                # Static API responses remain useful even if a read-only root
                # cannot receive the optional bundle copy.
                pass


def _sync_script_path() -> str | None:
    """Locate the bundled local handoff scanner in source or installed mode."""

    candidates = (
        os.path.join(BUNDLE_DIR, "vpm_sync.py"),
        os.path.join(os.path.dirname(os.path.dirname(BUNDLE_DIR)), "scripts", "vpm_sync.py"),
    )
    for candidate in candidates:
        if os.path.isfile(candidate) and not os.path.islink(candidate):
            return candidate
    return None


def _run_task_sync() -> None:
    """Run one bounded, private generator handoff scan in a daemon thread."""

    global SYNC_RUNNING
    _update_sync_status(state="running", last_scan=now())
    try:
        script = _sync_script_path()
        if not script:
            _update_sync_status(state="error", failed=1)
            return
        script_args = [
            script,
            "--no-start",
            "--root",
            ROOT,
            "--max-tasks",
            "25",
        ]
        if TASK_DIR:
            script_args.extend(["--task-dir", TASK_DIR])
        if TASK_MAP:
            script_args.extend(["--map", TASK_MAP])

        interpreters: list[list[str]] = []

        def add_interpreter(prefix: list[str]) -> None:
            if not prefix:
                return
            try:
                key = os.path.normcase(os.path.abspath(prefix[0]))
            except (OSError, TypeError, ValueError):
                key = str(prefix[0]).casefold()
            for existing in interpreters:
                try:
                    existing_key = os.path.normcase(os.path.abspath(existing[0]))
                except (OSError, TypeError, ValueError):
                    existing_key = str(existing[0]).casefold()
                if existing_key == key:
                    return
            interpreters.append(prefix)

        if os.name == "nt":
            # Prefer a normal launcher over embedded Python (for example the
            # LibreOffice runtime), then PATH interpreters and finally the
            # interpreter serving this process.  A failed candidate is
            # harmless because the scan is idempotent and retried later.
            launcher = shutil.which("py")
            if launcher:
                add_interpreter([launcher, "-3"])
            for name in ("python", "python3"):
                normal_python = shutil.which(name)
                if normal_python:
                    add_interpreter([normal_python])
        add_interpreter([sys.executable])

        for interpreter in interpreters:
            command = [*interpreter, *script_args]
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=manager_child_environment(),
                    timeout=SYNC_TIMEOUT,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if completed.returncode == 0:
                payload: dict[str, object] = {}
                # The scanner emits one compact JSON object.  Parse only the
                # last object and retain aggregate counts; task IDs, paths and
                # generator/provider details never leave the child process.
                for line in reversed((completed.stdout or "").splitlines()):
                    try:
                        candidate = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(candidate, dict):
                        payload = candidate
                        break
                received = payload.get("received")
                pending = payload.get("pending")
                try:
                    # Keep the public status an aggregate only.  ``scanned``
                    # is the number of task files inspected; older scanners
                    # only exposed ``eligible`` so retain that as a fallback.
                    scanned = max(0, min(10000, int(payload.get("scanned", payload.get("eligible", 0)) or 0)))
                except (TypeError, ValueError):
                    scanned = 0
                if isinstance(received, list):
                    received_count = len(received)
                else:
                    try:
                        received_count = int(payload.get("imported", received) or 0)
                    except (TypeError, ValueError):
                        received_count = 0
                # Prefer the scanner's uncapped pending_count when present;
                # the detailed pending list may intentionally be truncated.
                try:
                    pending_count = int(payload.get("pending_count"))
                except (TypeError, ValueError):
                    pending_count = len(pending) if isinstance(pending, list) else pending_handoff_count()
                unmatched_count, delivery_failed_count = _pending_row_counts(pending)
                # New scanners may expose aggregate category counts even when
                # the detail list is intentionally capped.  Prefer those
                # values, while retaining the row-derived fallback for older
                # bundles.
                try:
                    unmatched_count = max(
                        unmatched_count,
                        min(10000, int(payload.get("unmatched_count", payload.get("unmatched", 0)) or 0)),
                    )
                except (TypeError, ValueError):
                    pass
                try:
                    delivery_failed_count = max(
                        delivery_failed_count,
                        min(10000, int(payload.get("delivery_failed_count", payload.get("delivery_failed", 0)) or 0)),
                    )
                except (TypeError, ValueError):
                    pass
                try:
                    # ``failed`` in older scanner output meant pending rows;
                    # do not carry that overloaded value into the new public
                    # field.  A failed scanner process is represented by the
                    # error branch below instead.
                    failed = max(0, min(10000, int(payload.get("scan_failed", 0) or 0)))
                except (TypeError, ValueError):
                    failed = 0
                _update_sync_status(
                    state="watching",
                    last_scan=now(),
                    scanned=scanned,
                    received=received_count,
                    pending=max(0, min(10000, int(pending_count or 0))),
                    unmatched=unmatched_count,
                    delivery_failed=delivery_failed_count,
                    failed=failed,
                )
                break
        else:
            _update_sync_status(state="error", last_scan=now(), failed=1)
    except (OSError, subprocess.TimeoutExpired):
        _update_sync_status(state="error", last_scan=now(), failed=1)
    finally:
        with SYNC_STATE_LOCK:
            SYNC_RUNNING = False


def schedule_task_sync(force: bool = False) -> None:
    """Schedule an idempotent local handoff scan, never more often than needed."""

    global SYNC_RUNNING, SYNC_LAST_STARTED
    # A normal browser read is not a scan request.  Only schedule when the
    # bounded watcher fingerprint changed; explicit writes/manual refreshes
    # pass ``force=True`` and retain their immediate behaviour.
    if force:
        _watch_signature_changed(force=True)
    elif not _watch_signature_changed():
        return
    now_mono = time.monotonic()
    with SYNC_STATE_LOCK:
        if SYNC_RUNNING or (not force and now_mono - SYNC_LAST_STARTED < SYNC_MIN_INTERVAL):
            return
        SYNC_RUNNING = True
        SYNC_LAST_STARTED = now_mono
    threading.Thread(target=_run_task_sync, name="video-asset-manager-sync", daemon=True).start()


def _task_watcher_loop() -> None:
    """Keep receiving generic generator handoffs while the window is open."""

    interval = WATCH_INTERVAL
    while not WATCHER_STOP.wait(interval):
        try:
            changed = _watch_signature_changed()
        except (OSError, RuntimeError, TypeError, ValueError):
            # A malformed/temporarily unavailable handoff directory must not
            # terminate the long-lived watcher thread.  Back off and let the
            # next tick retry the bounded probe.
            changed = False
        with SYNC_STATE_LOCK:
            running = bool(SYNC_RUNNING)
            last_started = float(SYNC_LAST_STARTED or 0.0)
            sync_state = str(SYNC_STATUS.get("state") or "")
        try:
            pending = pending_handoff_count()
        except Exception:
            pending = 0
        retry_due = pending > 0 and time.monotonic() - last_started >= WATCH_RETRY_INTERVAL
        if changed or retry_due:
            # ``force`` is safe here: the fingerprint has already established
            # that work is worth attempting, and the scheduler still rejects
            # an overlapping scan.
            schedule_task_sync(force=True)
            interval = WATCH_INTERVAL
        elif running:
            interval = WATCH_INTERVAL
        elif pending > 0 or sync_state == "error":
            # Keep pending/failed handoffs responsive, but do not launch a
            # subprocess every tick when their files are unchanged.
            interval = WATCH_ERROR_INTERVAL
        else:
            interval = min(WATCH_IDLE_INTERVAL, max(WATCH_INTERVAL, interval * 1.5))


def start_task_watcher() -> None:
    """Start one process-local handoff watcher (idempotent)."""

    global WATCHER_THREAD
    if WATCHER_THREAD and WATCHER_THREAD.is_alive() and not WATCHER_STOP.is_set():
        return
    # If a previous test/embedded run stopped the watcher, wait briefly for
    # its daemon thread to leave before creating a replacement.  This avoids
    # two polling loops sharing the same process when ``main`` is exercised
    # repeatedly in one interpreter.
    if WATCHER_THREAD and WATCHER_THREAD.is_alive():
        WATCHER_THREAD.join(timeout=1.0)
        if WATCHER_THREAD.is_alive():
            return
    WATCHER_STOP.clear()
    WATCHER_THREAD = threading.Thread(
        target=_task_watcher_loop,
        name="video-asset-manager-watcher",
        daemon=True,
    )
    WATCHER_THREAD.start()


def stop_task_watcher() -> None:
    WATCHER_STOP.set()
    thread = WATCHER_THREAD
    if thread and thread is not threading.current_thread() and thread.is_alive():
        thread.join(timeout=1.0)


def manager_child_environment() -> dict[str, str]:
    """Return an environment safe for manager scanner/ffmpeg children."""

    try:
        return _privacy_manager_child_environment()  # type: ignore[name-defined]
    except NameError:  # old bundle fallback defined above
        child_env = os.environ.copy()
        for name in list(child_env):
            if re.search(
                r"(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|AUTH|CREDENTIAL|PRIVATE[_-]?KEY|SIGNATURE)",
                str(name), re.I,
            ):
                child_env.pop(name, None)
        return child_env


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def clip_media(doc: dict, clip_id: str, prefer_proxy: bool = True) -> str | None:
    for clip in doc.get("clips", []):
        if clip.get("id") != clip_id:
            continue
        if prefer_proxy and clip.get("proxy"):
            return norm_rel(clip["proxy"])
        current = clip.get("current")
        for version in clip.get("versions", []):
            if version.get("v") == current:
                return norm_rel(version.get("file"))
        if clip.get("versions"):
            return norm_rel(clip["versions"][-1].get("file"))
    return None


def ffmpeg_run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    result = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, timeout=timeout,
                            env=manager_child_environment())
    if result.returncode != 0 and os.environ.get("VAM_FFMPEG_DEBUG") == "1":
        # Diagnostic hook for failures that are impossible to reproduce
        # outside the server process; controlled by an explicit env var.
        try:
            with open(os.path.join(MODULE_DIR, "ffmpeg-debug.log"), "a", encoding="utf-8") as handle:
                handle.write("CMD: " + " ".join(repr(arg) for arg in args) + "\n")
                handle.write((result.stderr or b"").decode("utf-8", "replace")[-4000:] + "\n---\n")
        except OSError:
            pass
    return result


def _frame_luma(path: str) -> float:
    """Average luma of an image, used to reject black poster candidates."""

    try:
        result = subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-i", path, "-frames:v", "1",
             "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60, env=manager_child_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1.0
    for line in (result.stdout or "").splitlines():
        if "YAVG" in line:
            try:
                return float(line.split("=")[-1])
            except (TypeError, ValueError):
                return -1.0
    return -1.0


def make_poster(source: str, destination: str) -> bool:
    """Pick a representative frame for a poster.

    A timeline often opens on a black fade, and a fixed early timestamp produced
    an all-black thumbnail.  Probe a few moments and keep the brightest frame.
    """

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    best_score = -1.0
    best_path = None
    for index, stamp in enumerate(("2.0", "4.0", "1.0", "6.0", "0.5", "0.0")):
        candidate = f"{destination}.{index}.jpg"
        try:
            done = ffmpeg_run(["-ss", stamp, "-i", source, "-frames:v", "1",
                               "-vf", "scale=640:-2", candidate], 60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode != 0 or not os.path.isfile(candidate):
            continue
        score = _frame_luma(candidate)
        if score > best_score:
            if best_path and os.path.isfile(best_path):
                try:
                    os.unlink(best_path)
                except OSError:
                    pass
            best_score, best_path = score, candidate
        else:
            try:
                os.unlink(candidate)
            except OSError:
                pass
    if not best_path:
        return False
    try:
        os.replace(best_path, destination)
    except OSError:
        return False
    return True


def concat_reencode(files: list[str], output: str, width: int = 480, height: int = 854, crf: int = 30,
                    fps: int = 30, bitrate_kbps: int | None = None) -> tuple[bool, str]:
    """Normalize every input to one size/rate and join them.

    The join itself is a stream copy, so the delivery encoding (target bitrate,
    48 kHz AAC at 192 kbps) is applied to the normalized parts.
    """
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vam-ffmpeg-") as temp_dir:
        parts = []
        for index, source in enumerate(files):
            part = os.path.join(temp_dir, f"part-{index}.mp4")
            try:
                rate = int(fps) if int(fps) in TIMELINE_FPS else 30
                video_args = (["-b:v", f"{int(bitrate_kbps)}k", "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
                               "-bufsize", f"{int(bitrate_kbps) * 2}k"] if bitrate_kbps else ["-crf", str(crf)])
                result = ffmpeg_run(["-i", source, "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={rate}",
                                     "-c:v", "libx264", "-preset", "veryfast", *video_args,
                                     "-r", str(rate),
                                     "-c:a", "aac", "-b:a", EXPORT_AUDIO_BITRATE,
                                     "-ar", str(EXPORT_AUDIO_RATE), "-ac", "2", part], 600)
            except (OSError, subprocess.TimeoutExpired):
                return False, "media operation timed out"
            if result.returncode != 0:
                return False, f"unable to encode part {index + 1}"
            parts.append(part)
        list_file = os.path.join(temp_dir, "concat.txt")
        with open(list_file, "w", encoding="utf-8") as handle:
            for part in parts:
                handle.write("file '" + part.replace("'", "'\\''") + "'\n")
        try:
            result = ffmpeg_run(["-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output], 600)
        except (OSError, subprocess.TimeoutExpired):
            return False, "media operation timed out"
        return (result.returncode == 0, "ok" if result.returncode == 0 else "unable to join media")


def _probe_has_audio(source: str) -> bool:
    """Return whether a media file has an audio stream without exposing output."""

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        # ``ffmpeg -i`` probing is expensive and its diagnostics are private;
        # conservative false is safer than trying to map a missing stream.
        return False
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=index", "-of", "csv=p=0", source],
            capture_output=True, timeout=30, env=manager_child_environment(),
        )
        return bool(result.returncode == 0 and result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _probe_media_info(source: str | None) -> dict:
    """Read duration, frame rate and pixel size from the media itself.

    The output frame rate must come from media
    analysis rather than an assumed 30 fps, so one bounded ffprobe call returns
    everything the editor and the exporter need.
    """

    empty = {"duration": 0.0, "fps": 0, "width": 0, "height": 0}
    if not source or not os.path.isfile(source):
        return empty
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return empty
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate,avg_frame_rate,width,height",
             "-show_entries", "format=duration",
             "-of", "json", source],
            capture_output=True, text=True, timeout=30,
            env=manager_child_environment(),
        )
        if result.returncode != 0:
            return empty
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return empty
    stream = next((item for item in payload.get("streams", []) if isinstance(item, dict)), {})
    duration = _number((payload.get("format") or {}).get("duration"), 0.0)
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = str(stream.get(key) or "")
        if "/" in raw:
            numerator, _, denominator = raw.partition("/")
            try:
                top, bottom = float(numerator), float(denominator)
            except ValueError:
                continue
            if bottom > 0 and top > 0:
                fps = top / bottom
                break
        elif raw:
            fps = _number(raw, 0.0)
            if fps > 0:
                break
    info = {
        "duration": round(min(duration, 24 * 60 * 60), 6) if duration > 0 else 0.0,
        "fps": int(round(fps)) if fps > 0 else 0,
        "width": int(_number(stream.get("width"), 0) or 0),
        "height": int(_number(stream.get("height"), 0) or 0),
    }
    if info["fps"] not in TIMELINE_FPS:
        info["fps"] = min(TIMELINE_FPS, key=lambda value: abs(value - (info["fps"] or 30)))
    return info


def _probe_media_duration(source: str | None) -> float:
    """Read a media duration for edit validation without exposing probe output."""

    return _probe_media_info(source)["duration"]


_MEDIA_DURATION_CACHE: dict[str, tuple[int, int, float]] = {}
_MEDIA_DURATION_CACHE_LOCK = threading.Lock()
# Picture-only durations of rendered segments: the container can be longer than
# the video stream, and xfade measures its inputs by real picture length.
_VIDEO_DURATION_CACHE: dict[str, tuple[int, int, float]] = {}
_VIDEO_DURATION_CACHE_LOCK = threading.Lock()
_VIDEO_DURATION_CACHE_MAX = 200
# The reusable-asset library is expensive to rebuild (every manifest plus poster
# derivation), so it is reused while its signature holds — no time limit.
_LIBRARY_CACHE: dict | None = None
_LIBRARY_CACHE_SIG: tuple | None = None
_LIBRARY_CACHE_LOCK = threading.RLock()


def _cached_media_info(source: str | None) -> dict:
    """Probe a local media file once per unchanged (mtime, size) signature."""

    empty = {"duration": 0.0, "fps": 0, "width": 0, "height": 0}
    if not source or not os.path.isfile(source):
        return dict(empty)
    try:
        stat = os.stat(source)
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return dict(empty)
    key = os.path.realpath(source)
    with _MEDIA_DURATION_CACHE_LOCK:
        cached = _MEDIA_DURATION_CACHE.get(key)
        if cached and cached[:2] == signature:
            return dict(cached[2]) if isinstance(cached[2], dict) else dict(empty)
    info = _probe_media_info(source)
    with _MEDIA_DURATION_CACHE_LOCK:
        _MEDIA_DURATION_CACHE[key] = (signature[0], signature[1], info)
    return dict(info)


def _cached_media_duration(source: str | None) -> float:
    """Probe a local media file once per unchanged (mtime, size) signature."""

    return _cached_media_info(source)["duration"]


def _hydrate_clip_media_durations(document: dict, slug: str) -> None:
    """Fill missing public clip/version durations from bounded local files.

    This is deliberately an in-memory normalization step.  It keeps legacy
    manifests readable and fixes the UI immediately without rewriting a
    project on every GET; normal delivery/trim writes persist the same values.
    """

    if not isinstance(document, dict) or not SLUG_RE.fullmatch(str(slug or "")):
        return
    clips = document.get("clips")
    if not isinstance(clips, list):
        return
    # A source commonly appears in both media[] and clips[]. Probe/check that
    # path once within this normalization, while retaining fresh checks on the
    # next call (including files replaced without a manifest edit).
    media_info: dict[str, dict] = {}

    def info_for(rel: str | None) -> dict:
        if not rel:
            return _cached_media_info(None)
        if rel not in media_info:
            media_info[rel] = _cached_media_info(safe_file(slug, rel))
        return media_info[rel]

    for clip in clips:
        if not isinstance(clip, dict):
            continue
        versions = clip.get("versions")
        if not isinstance(versions, list):
            continue
        current = clip.get("current")
        current_duration = 0.0
        for version in versions:
            if not isinstance(version, dict):
                continue
            rel = norm_rel(version.get("file"))
            duration = info_for(rel)["duration"]
            if duration <= 0:
                duration = _number(version.get("duration") or version.get("length"), 0.0)
            if duration <= 0:
                continue
            duration = round(min(duration, 24 * 60 * 60), 6)
            version["duration"] = duration
            try:
                is_current = int(version.get("v")) == int(current)
            except (TypeError, ValueError, OverflowError):
                is_current = False
            if is_current:
                current_duration = duration
        if current_duration > 0:
            clip["duration"] = current_duration
    # Media records carry their own probe results (duration, frame rate, size),
    # and the project frame rate follows the FIRST video -- a later asset never
    # lets a later asset override the size/rate the project started with.
    media = document.get("asset", {}).get("media") if isinstance(document.get("asset"), dict) else None
    first_video_fps = 0
    if isinstance(media, list):
        for record in media:
            if not isinstance(record, dict):
                continue
            rel = norm_rel(record.get("file"))
            info = info_for(rel)
            if info["duration"] > 0 and _number(record.get("duration"), 0.0) <= 0:
                record["duration"] = info["duration"]
            if info["fps"] > 0 and not _number(record.get("fps"), 0):
                record["fps"] = info["fps"]
            if info["width"] > 0 and not _number(record.get("width"), 0):
                record["width"] = info["width"]
            if info["height"] > 0 and not _number(record.get("height"), 0):
                record["height"] = info["height"]
            if not first_video_fps and info["fps"] > 0 and str(record.get("kind") or "video") == "video":
                first_video_fps = info["fps"]
    assembly = document.get("assembly") if isinstance(document.get("assembly"), dict) else None
    timeline = assembly.get("timeline") if isinstance(assembly, dict) and isinstance(assembly.get("timeline"), dict) else None
    if timeline is not None and first_video_fps:
        current_fps = timeline.get("fps")
        try:
            numeric_fps = int(current_fps)
        except (TypeError, ValueError):
            numeric_fps = 0
        if numeric_fps == 0 or numeric_fps == 30 or numeric_fps not in TIMELINE_FPS:
            timeline["fps"] = first_video_fps


def _timeline_media_source(document: dict, item: dict, slug: str) -> str | None:
    """Resolve a timeline reference to a safe local file."""

    clip_id = item.get("clip_id")
    if clip_id:
        clip = next((value for value in document.get("clips", [])
                     if isinstance(value, dict) and value.get("id") == clip_id), None)
        if not isinstance(clip, dict):
            return None
        version = _version_for_clip(clip, item.get("version"))
        rel = version.get("file") if isinstance(version, dict) else clip_media(document, str(clip_id), False)
    else:
        media_id = item.get("media_id")
        media = next((value for value in (document.get("asset", {}).get("media", []) or [])
                      if isinstance(value, dict) and value.get("id") == media_id), None)
        rel = media.get("file") if isinstance(media, dict) else None
    return safe_file(slug, rel)


def _probe_video_duration(source: str | None) -> float:
    """Video-stream duration of a local file, or 0 when it cannot be measured.

    ``_probe_media_duration`` reports the container, which is the longest stream;
    a rendered segment can carry a slightly longer audio track than picture, and
    xfade measures its inputs by their real picture length.
    """

    if not source or not os.path.isfile(source):
        return 0.0
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    key = os.path.abspath(source)
    try:
        stat = os.stat(key)
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return 0.0
    with _VIDEO_DURATION_CACHE_LOCK:
        cached = _VIDEO_DURATION_CACHE.get(key)
    if isinstance(cached, tuple) and cached[:2] == signature:
        return float(cached[2])
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", source],
            capture_output=True, text=True, timeout=60, env=manager_child_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    try:
        value = float((result.stdout or "").strip().splitlines()[0])
    except (IndexError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    with _VIDEO_DURATION_CACHE_LOCK:
        if len(_VIDEO_DURATION_CACHE) >= _VIDEO_DURATION_CACHE_MAX:
            _VIDEO_DURATION_CACHE.clear()
        _VIDEO_DURATION_CACHE[key] = (signature[0], signature[1], value)
    return value


def _timeline_item_label(item: dict, index: int, track_id: str) -> str:
    """Plain-text identity of one timeline item, for customer-facing errors."""

    reference = str(item.get("clip_id") or item.get("media_id") or "").strip()
    start = max(0.0, _number(item.get("start"), 0.0))
    label = f"{track_id} clip {index + 1}"
    if reference:
        label += f" ({reference})"
    return label + f" at {start:.2f}s"


def _timeline_preflight(document: dict, timeline: dict, slug: str) -> dict[str, list[str]]:
    """Split unusable timeline media into blocking problems and warnings.

    Only the lane that becomes the main picture can make the render fail: the
    finishing pass simply skips an overlay or audio item whose media cannot be
    resolved (``server.py`` overlay collection filters on the resolved source),
    so those stay warnings and never block a render that works today.  A hidden
    main lane is not rendered at all, and is therefore not a problem either.
    """

    tracks = _timeline_tracks(timeline)
    lanes = [(track_id, kind) for track_id, kind in TIMELINE_TRACKS.items()
             if kind in {"video", "audio"}]
    main_track_id = "video-main"
    main_track = tracks.get(main_track_id) or {}
    if not [item for item in (main_track.get("clips") or []) if isinstance(item, dict)]:
        for candidate in ("video-overlay", "video-2", "video-3"):
            track = tracks.get(candidate) or {}
            if [item for item in (track.get("clips") or []) if isinstance(item, dict)]:
                main_track_id = candidate
                break
    main_hidden = bool((tracks.get("video-main") or {}).get("hidden"))
    blocking: list[str] = []
    warnings: list[str] = []
    for track_id, _ in lanes:
        track = tracks.get(track_id) or {}
        if track.get("hidden"):
            continue
        items = [item for item in (track.get("clips") or []) if isinstance(item, dict)]
        for index, item in enumerate(items[:TIMELINE_MAX_ITEMS]):
            if _timeline_media_source(document, item, slug):
                continue
            message = (f"{_timeline_item_label(item, index, track_id)}: its media file is "
                       "missing, unreadable, or outside the project")
            if track_id == main_track_id and not (main_hidden and track_id == "video-main"):
                blocking.append(message)
            else:
                warnings.append(message)
    return {"blocking": blocking, "warnings": warnings}


def _encode_failure_reason(stderr: bytes | str) -> str:
    """Turn an encoder rejection into one actionable, path-free sentence.

    Raw ffmpeg output names host paths, so only recognised causes are reported;
    the full text stays available through the ``VAM_FFMPEG_DEBUG`` log hook.
    """

    text = stderr.decode("utf-8", "replace") if isinstance(stderr, (bytes, bytearray)) else str(stderr or "")
    lowered = text.casefold()
    hints = (
        ("padded dimensions cannot be smaller than input dimensions",
         "the clip's scale or position pushes the picture outside the canvas"),
        ("no such file or directory", "the source file could not be opened"),
        ("invalid data found", "the source file is not readable as media"),
        ("does not contain any stream", "the source file has no usable video stream"),
        ("cannot allocate memory", "the machine ran out of memory while encoding"),
        ("conversion failed", "the source file could not be decoded"),
        ("invalid argument", "the encoder rejected this clip's settings"),
    )
    for needle, hint in hints:
        if needle in lowered:
            return hint
    return "the encoder rejected this clip's source or settings"


def _ffmpeg_filter_path(path: str) -> str:
    """Escape a native path for FFmpeg's filter argument parser."""

    value = os.path.abspath(path).replace("\\", "/")
    value = value.replace("'", r"\\'")
    if len(value) > 1 and value[1] == ":":
        value = value[0] + "\\:" + value[2:]
    return value


def _ass_escape(value: object) -> str:
    """Escape public caption text for an ASS dialogue line."""

    text_value = str(value or "").replace("\\", r"\\").replace("{", r"\\{").replace("}", r"\\}")
    return text_value.replace("\r\n", r"\\N").replace("\n", r"\\N").replace("\r", r"\\N")


def _ass_timestamp(seconds: float) -> str:
    total = max(0.0, float(seconds or 0.0))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _ass_color(value: str | None, alpha_bits: str = "00") -> str:
    """Convert #RRGGBB to ASS &H<AABBGGRR> (alpha hex bits given)."""

    text = (value or "").strip().lstrip("#").lower()
    if not re.fullmatch(r"[0-9a-f]{6}", text):
        return f"&H{alpha_bits}FFFFFF"
    return f"&H{alpha_bits}{text[4:6]}{text[2:4]}{text[0:2]}"


def _ass_fade_tag(fade_ms: int, span_seconds: float, easing: object) -> str:
    """Return the ASS override tag for a caption fade-in/out.

    ``\\fad`` is a linear alpha envelope and stays the default.  The
    Animation tab also offers eased curves; libass expresses those with
    animated alpha (``\\t``) whose acceleration parameter approximates the
    curve: accel > 1 starts slowly (ease-in) and accel < 1 starts quickly
    (ease-out).  ease-in-out is emitted as two consecutive segments.
    """

    if fade_ms <= 0:
        return ""
    curve = str(easing or "linear").strip().lower()
    if curve not in CAPTION_EASINGS or curve == "linear":
        return f"\\fad({fade_ms},{fade_ms})"
    total = max(fade_ms * 2, int(round(max(0.01, float(span_seconds)) * 1000)))
    out_start = max(fade_ms, total - fade_ms)
    if curve == "ease-in":
        segments = [(0, fade_ms, 2.0), (out_start, total, 0.5)]
    elif curve == "ease-out":
        segments = [(0, fade_ms, 0.5), (out_start, total, 2.0)]
    else:
        half = max(1, fade_ms // 2)
        segments = [
            (0, half, 2.0), (half, fade_ms, 0.5),
            (out_start, min(total, out_start + half), 2.0),
            (min(total, out_start + half), total, 0.5),
        ]
    tags = "\\alpha&HFF&"
    for index, (start_ms, end_ms, accel) in enumerate(segments):
        if end_ms <= start_ms:
            continue
        target = "\\alpha&H00&" if index == 0 or (curve == "ease-in-out" and index < 2) else "\\alpha&HFF&"
        if curve == "ease-in-out" and index >= 2:
            target = "\\alpha&HFF&"
        tags += f"\\t({start_ms},{end_ms},{accel:.2f},{target})"
    return tags


def _write_timeline_ass(timeline: dict, destination: str, width: int, height: int) -> bool:
    """Write a bounded, customer-visible subtitle/title track for libass.

    One ASS style is generated per distinct style combination (font, size,
    colors, outline, background box, alignment) so arbitrary per-cue styling
    required by the spec — font/color/outline/background/position, plus
    title-style records — burns exactly like the live canvas overlay.
    """

    tracks = _timeline_tracks(timeline)
    text_track = tracks.get("text-main") or {}
    cues = [] if text_track.get("hidden") else text_track.get("cues", [])
    cues = [cue for cue in cues if isinstance(cue, dict) and str(cue.get("text") or "").strip()]
    if not cues:
        return False
    # CSS-ish palette helpers shared with the frontend defaults.
    styles: list[dict] = []
    style_index: dict[tuple, int] = {}

    def style_key(cue: dict) -> tuple:
        style = cue.get("style") if isinstance(cue.get("style"), dict) else {}
        kind = str(cue.get("kind") or "subtitle")
        return (
            str(style.get("position") or "bottom"),
            round(float(_number(style.get("posX"), 50.0)), 2),
            round(float(_number(style.get("posY"), {"top": 16, "center": 50, "bottom": 84}.get(str(style.get("position") or "bottom"), 84))), 2),
            str(style.get("color") or "#ffffff"),
            str(style.get("font") or "Arial"),
            int(_number(style.get("fontSize"), 96 if kind == "title" else 64)),
            str(style.get("outlineColor") or "#101010"),
            int(_number(style.get("outlineWidth"), 6 if kind == "title" else 4)),
            str(style.get("background") or ""),
            float(_number(style.get("backgroundOpacity"), 0.35 if style.get("background") else 0.0)),
        )

    for cue in cues:
        key = style_key(cue)
        if key not in style_index:
            if len(styles) >= 8:
                continue
            style_index[key] = len(styles)
            position = key[0] if key[0] in _CAPTION_POSITIONS else "bottom"
            pos_x, pos_y = key[1], key[2]
            if position == "top":
                alignment, margin_v = 8, 44
            elif position == "center":
                alignment, margin_v = 5, max(0, int(height // 2 - key[5] // 2))
            else:
                alignment, margin_v = 2, 90
            has_bg = bool(key[8])
            bg_alpha = max(0, min(255, int(round((1.0 - key[9]) * 255))))
            styles.append({
                "name": f"S{style_index[key]}",
                "font": key[4], "size": key[5],
                "primary": _ass_color(key[3]),
                "outline": _ass_color(key[6]),
                "background": _ass_color(key[8] or "#000000", f"{bg_alpha:02X}"),
                "border_style": 3 if has_bg else 1,
                "outline_width": key[7],
                "alignment": alignment, "margin_v": margin_v,
                "pos_x": pos_x, "pos_y": pos_y,
            })

    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {int(width)}", f"PlayResY: {int(height)}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for style in styles:
        header.append(
            f"Style: {style['name']},{style['font']},{style['size']},{style['primary']},"
            f"&H00FFFFFF&,{style['outline']},{style['background']},0,0,0,0,100,100,0,0,"
            f"{style['border_style']},{style['outline_width']},1,{style['alignment']},60,60,{style['margin_v']},1"
        )
    header.append("")
    header.extend(["[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"])
    for cue in cues[:TIMELINE_MAX_CUES]:
        start = max(0.0, _number(cue.get("start"), 0.0))
        end = max(start + 0.01, _number(cue.get("end"), start + 1.0))
        key = style_key(cue)
        name = styles[style_index[key]]["name"] if key in style_index else "S0"
        text_value = _ass_escape(cue.get("text"))
        style = next((entry for entry in styles if entry.get("name") == name), None) or {}
        x = int(round(width * float(style.get("pos_x", 50.0)) / 100.0))
        y = int(round(height * float(style.get("pos_y", 84.0)) / 100.0))
        cue_style = cue.get("style") if isinstance(cue.get("style"), dict) else {}
        cue_motion = str(cue_style.get("motion") or "fade").strip().lower()
        cue_fade = 0.0 if cue_motion == "none" else max(0.0, min(2.0, _number(cue_style.get("animation"), 0.0)))
        fade_ms = int(round(min((end - start) / 2, cue_fade) * 1000))
        fade_tag = _ass_fade_tag(fade_ms, end - start, cue_style.get("easing"))
        header.append(f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},{name},,0,0,0, ,{{\\pos({x},{y}){fade_tag}}}{text_value}")
    try:
        with open(destination, "w", encoding="utf-8-sig", newline="\n") as handle:
            handle.write("\n".join(header) + "\n")
        return True
    except OSError:
        return False


def _render_timeline_finish(base_input: str, output: str, document: dict, slug: str,
                            timeline: dict, width: int, height: int, crf: int,
                            temp_dir: str, timeline_duration: float | None = None,
                            fps: int = 30, quality: str = "recommended", main_track_id: str = "video-main") -> tuple[bool, str]:
    """Apply captions and the standalone audio track to the rendered video."""

    def _audio_item_chain(item: dict, limit: float) -> str:
        """atrim/in-out/speed/gain/fades/delay for one timeline item's own audio."""

        start = max(0.0, _number(item.get("in"), 0.0))
        end = max(start + 0.01, _number(item.get("out"), start + _number(item.get("duration"), 1.0)))
        end = min(end, start + limit)
        speed = max(0.25, min(4.0, _number(item.get("speed"), 1.0)))
        chain = [f"atrim=start={start:.6f}:end={end:.6f}", "asetpts=PTS-STARTPTS"]
        tempo = _atempo_chain(speed)
        if tempo:
            chain.append(tempo)
        gain = _number(item.get("gain"), 0.0)
        if abs(gain) > 0.001:
            chain.append(f"volume={gain:.3f}dB")
        fade_in = max(0.0, _number(item.get("fade_in"), 0.0))
        fade_out = max(0.0, _number(item.get("fade_out"), 0.0))
        if fade_in > 0:
            chain.append(f"afade=t=in:st=0:d={fade_in:.6f}")
        if fade_out > 0:
            span = max(0.01, (end - start) / speed)
            chain.append(f"afade=t=out:st={max(0.0, span - fade_out):.6f}:d={fade_out:.6f}")
        delay = max(0, int(round(max(0.0, _number(item.get("start"), 0.0)) * 1000)))
        chain.append(f"adelay={delay}|{delay}")
        return ",".join(chain)

    tracks = _timeline_tracks(timeline)
    cues = (tracks.get("text-main") or {}).get("cues") or []
    overlay_track = tracks.get("video-overlay") or {}
    overlay_items: list[tuple[dict, dict]] = [] if overlay_track.get("hidden") else [
        (item, overlay_track) for item in (overlay_track.get("clips") or []) if isinstance(item, dict)]
    # Additional overlay video tracks (multi-track overlay requirement).
    for extra_track_id in ("video-2", "video-3", "video-overlay"):
        extra_track = tracks.get(extra_track_id) or {}
        if extra_track_id == main_track_id:
            continue
        if not extra_track.get("hidden"):
            overlay_items.extend((item, extra_track) for item in (extra_track.get("clips") or []) if isinstance(item, dict))
    max_duration = max(0.01, _number(timeline_duration, 0.01))
    main_track_state = tracks.get(main_track_id) or {}
    main_track_muted = bool(main_track_state.get("muted"))
    main_track_hidden = bool(main_track_state.get("hidden"))
    audio_track = tracks.get("audio-main") or {}
    audio_items = [] if audio_track.get("muted") or audio_track.get("hidden") else [
        item for item in (audio_track.get("clips") or [])
        if isinstance(item, dict) and _number(item.get("start"), 0.0) < max_duration]
    ass_path = os.path.join(temp_dir, "captions.ass")
    has_captions = bool(cues) and _write_timeline_ass(timeline, ass_path, width, height)
    usable_audio: list[tuple[dict, str]] = []
    for item in audio_items[:TIMELINE_MAX_ITEMS]:
        source = _timeline_media_source(document, item, slug)
        if source and _probe_has_audio(source):
            usable_audio.append((item, source))

    # Resolve overlays before deciding whether this is a no-op.  A timeline
    # with only a picture-in-picture layer still needs a compositor pass; the
    # old early return copied the base video and silently discarded overlays.
    usable_overlays: list[tuple[dict, str]] = []
    # (item, input slot) for overlay lanes whose soundtrack should be heard: a
    # muted lane still paints, it just does not join the mix.
    overlay_audio_slots: list[tuple[dict, int]] = []
    for item, item_track in overlay_items[:TIMELINE_MAX_ITEMS]:
        source = _timeline_media_source(document, item, slug)
        if source:
            slot = 1 + len(usable_overlays)
            usable_overlays.append((item, source))
            if not (item_track.get("muted") or item_track.get("hidden")):
                overlay_audio_slots.append((item, slot))

    if (not has_captions and not usable_audio and not usable_overlays
            and not main_track_muted and not main_track_hidden):
        try:
            shutil.copyfile(base_input, output)
            save_export_quality(slug, quality)
            return True, "ok"
        except OSError:
            return False, "unable to write render output"

    args: list[str] = ["-i", base_input]
    for _, source in usable_overlays:
        args.extend(["-i", source])
    for _, source in usable_audio:
        args.extend(["-i", source])
    filters: list[str] = []
    video_label = "0:v"
    main_items = [item for item in ((tracks.get(main_track_id) or {}).get("clips") or []) if isinstance(item, dict)]
    leading = min((max(0.0, _number(item.get("start"), 0.0)) for item in main_items), default=0.0)
    # ``_render_timeline_file`` materializes gaps (including a leading gap)
    # before calling this finishing pass.  Padding the base video again here
    # duplicated the leading gap whenever a clip had been moved down the
    # timeline, making the monitor/export appear to jump or lose the clip.
    # Keep ``leading`` for delaying standalone audio, but never add a second
    # video pad at this stage.
    # If an audio/overlay/subtitle track extends beyond the last video frame,
    # clone the final video frame before applying the overlay/caption graph.
    # The previous order built overlays first and then reset ``video_label`` to
    # the padded base, silently dropping the overlay output (and sometimes
    # causing FFmpeg to reject the unconnected filter graph).
    base_duration = _probe_media_duration(base_input)
    if max_duration > base_duration + 0.01:
        stop_duration = max(0.01, max_duration - max(0.0, base_duration))
        filters.append(f"[0:v]tpad=stop_mode=clone:stop_duration={stop_duration:.6f}[basev]")
        video_label = "basev"
    for index, (item, _) in enumerate(usable_overlays, start=1):
        next_label = f"vo{index}"
        in_point = max(0.0, _number(item.get("in"), 0.0))
        out_point = max(in_point + 0.01, _number(item.get("out"), in_point + _number(item.get("duration"), 1.0)))
        speed = max(0.25, min(4.0, _number(item.get("speed"), 1.0)))
        transform = item.get("transform") if isinstance(item.get("transform"), dict) else {}
        x = _number(transform.get("x"), 0.0)
        y = _number(transform.get("y"), 0.0)
        rotate = _number(transform.get("rotate"), 0.0)
        scale = max(0.05, min(20.0, _number(transform.get("scale"), 0.35)))
        # Legacy timelines stored the generic video default (scale=1) for a
        # new PIP item.  If the position is also untouched, interpret it as
        # the standard visible PIP size instead of covering the main canvas.
        if abs(scale - 1.0) < 1e-6 and abs(x) < 1e-6 and abs(y) < 1e-6 and abs(rotate) < 1e-6:
            scale = 0.35
        start = max(0.0, _number(item.get("start"), 0.0))
        chain = [f"trim=start={in_point:.6f}:end={out_point:.6f}", "setpts=PTS-STARTPTS"]
        if abs(speed - 1.0) > 1e-6:
            chain.append(f"setpts=PTS/{speed:.6f}")
        # PIP size is a fraction of the canvas SHORT side, matching the live
        # canvas monitor exactly.  A literal
        # width keeps ffmpeg's scale expression independent of the source
        # resolution, so the exported inset is identical to what the editor
        # shows even for a 480p or 4K source clip.
        pip_target_w = max(64, int(min(width, height) * scale / 2) * 2)
        chain.append(f"scale=w={pip_target_w}:h=-2:force_original_aspect_ratio=decrease")
        # Layer-level opacity mirrors the live canvas compositor.  A border width
        # is optional and defaults to off, so the inset stays a plain
        # picture-in-picture.
        opacity = max(0.05, min(1.0, _number(transform.get("opacity"), 1.0)))
        if opacity < 0.999:
            chain.append(f"format=rgba,colorchannelmixer=aa={opacity:.3f}")
        border = max(0.0, min(12.0, _number(transform.get("border"), 0.0)))
        if border > 0.001:
            chain.append(f"drawbox=x=0:y=0:w=iw-1:h=ih-1:color=white@0.9:t={border:.0f}")
        chain.append(f"setpts=PTS+{start:.6f}/TB")
        filters.append(f"[{index}:v]{','.join(chain)}[ov{index}]" )
        x_expr = f"(W-w)/2+{x:.3f}"
        y_expr = f"(H-h)/2+{y:.3f}"
        filters.append(f"[{video_label}][ov{index}]overlay=x={x_expr}:y={y_expr}:enable='between(t,{start:.6f},{start + max(0.01, (out_point-in_point)/speed):.6f})'[{next_label}]")
        video_label = next_label
    if has_captions:
        caption_label = "vcaption"
        fonts_dir = os.path.join(ROOT, "webapp")
        if not os.path.isdir(fonts_dir):
            fonts_dir = MODULE_DIR
        filters.append(f"[{video_label}]subtitles='{_ffmpeg_filter_path(ass_path)}':fontsdir='{_ffmpeg_filter_path(fonts_dir)}'[{caption_label}]")
        video_label = caption_label

    audio_label = "0:a"
    mix_inputs: list[str] = []
    hidden_main_audio = [(item, _timeline_media_source(document, item, slug))
                         for item in (main_items or [])[:TIMELINE_MAX_ITEMS]] \
        if (main_track_hidden and not main_track_muted) else []
    hidden_main_audio = [(item, source) for item, source in hidden_main_audio
                         if source and _probe_has_audio(source)]
    if usable_audio or overlay_audio_slots or hidden_main_audio:
        # The rendered base carries the primary soundtrack only while that track is
        # visible; a hidden or muted primary track contributes nothing here, and a
        # hidden one contributes its clips individually further down.  Keeping the
        # mix's single output connected matters: mapping a separate silence used to
        # break the filter graph.
        if not main_track_muted and not main_track_hidden:
            # The base movie already contains the leading gap as real frames and
            # silence, so its soundtrack is in sync as it stands: delaying it by the
            # first clip's start shifted the audio a second time (a clip starting at
            # 2.96s played silent while its sound appeared at ~5.9s).
            audio_prefix = "[0:a]aresample=44100,asetpts=PTS-STARTPTS"
            filters.append(audio_prefix + ",apad[basea]")
            mix_inputs = ["[basea]"]
        audio_input_offset = 1 + len(usable_overlays)
        for index, (item, _) in enumerate(usable_audio, start=audio_input_offset):
            out_label = f"mix{index}"
            start = max(0.0, _number(item.get("in"), 0.0))
            end = max(start + 0.01, _number(item.get("out"), start + _number(item.get("duration"), 1.0)))
            end = min(end, start + max_duration)
            speed = max(0.25, min(4.0, _number(item.get("speed"), 1.0)))
            chain = [f"atrim=start={start:.6f}:end={end:.6f}", "asetpts=PTS-STARTPTS"]
            tempo = _atempo_chain(speed)
            if tempo:
                chain.append(tempo)
            gain = _number(item.get("gain"), 0.0)
            if abs(gain) > 0.001:
                chain.append(f"volume={gain:.3f}dB")
            fade_in = max(0.0, _number(item.get("fade_in"), 0.0))
            fade_out = max(0.0, _number(item.get("fade_out"), 0.0))
            if fade_in > 0:
                chain.append(f"afade=t=in:st=0:d={fade_in:.6f}")
            if fade_out > 0:
                duration = max(0.01, (end - start) / speed)
                chain.append(f"afade=t=out:st={max(0.0, duration-fade_out):.6f}:d={fade_out:.6f}")
            delay = max(0, int(round(max(0.0, _number(item.get("start"), 0.0)) * 1000)))
            chain.append(f"adelay={delay}|{delay}")
            filters.append(f"[{index}:a]{','.join(chain)}[{out_label}]")
            mix_inputs.append(f"[{out_label}]")
        # Overlay (picture-in-picture) lanes keep their own sound; their sources
        # are already inputs, so the chain reuses that slot.
        for item, slot in overlay_audio_slots:
            out_label = f"mixov{slot}"
            filters.append(f"[{slot}:a]{_audio_item_chain(item, max_duration)}[{out_label}]")
            mix_inputs.append(f"[{out_label}]")
        # A hidden primary track still plays: only its picture is hidden.
        for offset, (item, main_source) in enumerate(hidden_main_audio):
            slot = audio_input_offset + len(usable_audio) + offset
            args.extend(["-i", main_source])
            out_label = f"mixmain{slot}"
            filters.append(f"[{slot}:a]{_audio_item_chain(item, max_duration)}[{out_label}]")
            mix_inputs.append(f"[{out_label}]")
        audio_label = "mixeda"
        filters.append("".join(mix_inputs) + f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=0,aresample=44100[{audio_label}]")
    elif has_captions or main_track_muted:
        audio_label = "0:a"
        # (the base soundtrack needs no delay: see the [basea] note above)
    if (main_track_muted or main_track_hidden) and not mix_inputs:
        # Nothing but the muted primary track carried sound, so the delivered file
        # keeps no audio track instead of a synthesized silent one.
        audio_label = None

    # Any overlay or caption changes the video label.  Mapping the original
    # input whenever captions are absent used to make a picture-in-picture
    # edit appear to save successfully while exporting the unedited video.
    video_map = f"[{video_label}]" if video_label != "0:v" else "0:v:0"
    if audio_label is None:
        audio_map = None
    else:
        # Every audio label produced here is a filter output except the raw "0:a" case.
        audio_map = f"[{audio_label}]" if audio_label != "0:a" else audio_label
    target_kbps = export_bitrate_kbps(width, height, fps, quality)
    # A muted primary track with no captions, overlays, or standalone audio leaves
    # the filter list empty, and ffmpeg rejects an empty ``-filter_complex``: the
    # export of such a timeline used to fail with "unable to finalize timeline".
    if filters:
        args.extend(["-filter_complex", ";".join(filters)])
    args.extend(["-map", video_map])
    if audio_map:
        args.extend(["-map", audio_map, "-c:a", "aac", "-b:a", EXPORT_AUDIO_BITRATE,
                     "-ar", str(EXPORT_AUDIO_RATE), "-ac", "2"])
    args.extend(["-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
                 "-b:v", f"{target_kbps}k", "-maxrate", f"{int(target_kbps * 1.5)}k",
                 "-bufsize", f"{target_kbps * 2}k",
                 "-t", f"{max_duration:.6f}", "-movflags", "+faststart", output])
    try:
        result = ffmpeg_run(args, 900)
    except (OSError, subprocess.TimeoutExpired):
        return False, "render timed out"
    return (result.returncode == 0 and os.path.isfile(output),
            "ok" if result.returncode == 0 else "unable to finalize timeline")


def save_export_quality(slug: str, quality: object) -> None:
    """Remember the chosen export quality on the project itself.

    The spec keeps the quality per project, so the next export dialog opens on the
    same choice.  Failures are ignored: persistence must never fail a render.
    """

    value = normalize_quality(quality)
    try:
        with LOCK:
            raw = read_json(project_file(slug), None)
            if not isinstance(raw, dict):
                return
            document = normalize_project(raw, slug)
            presets = document.setdefault("presets", {})
            if not isinstance(presets, dict) or presets.get("export_quality") == value:
                return
            presets["export_quality"] = value
            try:
                document["rev"] = max(0, int(document.get("rev", 0))) + 1
            except (TypeError, ValueError, OverflowError):
                document["rev"] = 1
            document["updated"] = now()
            write_json_atomic(project_file(slug), document)
    except (OSError, ValueError, TypeError):
        return


def _render_timeline_file(document: dict, slug: str, timeline: dict,
                          output: str, preset: dict) -> tuple[bool, str]:
    """Render the primary timeline, with optional fade cross-dissolves.

    Sources are normalized into a temporary, self-contained MP4 first. This
    makes heterogeneous generator outputs and missing audio predictable. The
    The editor materializes the primary video track and then applies overlay,
    caption, and standalone-audio layers in a bounded local compositor pass.
    """

    if not has_ffmpeg():
        return False, "ffmpeg unavailable"
    tracks = _timeline_tracks(timeline)
    # The bottom-most video row that actually holds a clip is the background; the
    # rows above it are picture-in-picture layers.  The search therefore runs
    # bottom-up (video-overlay, video-2, video-3), so a clip parked on an overlay
    # lane still renders as the main picture with its own audio instead of
    # failing with "no video clips", and the editor resolves the same lane.
    main_track_id = "video-main"
    main = tracks.get(main_track_id) or {}
    items = main.get("clips") if isinstance(main.get("clips"), list) else []
    if not items:
        for candidate in ("video-overlay", "video-2", "video-3"):
            track = tracks.get(candidate) or {}
            clips = [item for item in (track.get("clips") or []) if isinstance(item, dict)]
            if clips:
                main, items, main_track_id = track, clips, candidate
                break
    items = [item for item in items if isinstance(item, dict)]
    main_hidden = bool(main.get("hidden"))
    if main_hidden:
        items = []
    items.sort(key=lambda item: (item.get("start", 0.0), str(item.get("id"))))
    if not items:
        # A hidden primary track is a valid canvas state: render a bounded
        # background-only movie instead of failing the export.
        if not main_hidden:
            return False, "timeline has no video clips"
        ratio = str((timeline.get("canvas") or {}).get("ratio") or preset.get("ratio") or "9:16")
        width, height = TIMELINE_RATIOS.get(ratio, (1080, 1920))
        duration = max(0.1, min(24 * 60 * 60, _number(timeline.get("workspace_duration"), 7.0)))
        bg = str((timeline.get("canvas") or {}).get("background") or "#000000").lower()
        if not HEX_COLOR_RE.fullmatch(bg):
            bg = "#000000"
        fps = int(timeline.get("fps", 30)) if str(timeline.get("fps", 30)).isdigit() else 30
        if fps not in TIMELINE_FPS:
            fps = 30
        try:
            crf = int(preset.get("crf") or 20)
            quality = normalize_quality(preset.get("quality"))
        except (TypeError, ValueError, OverflowError):
            return False, "invalid render preset"
        # The background is the base of a normal compositor pass now: hiding a track
        # removes its picture, not its sound, so the finish pass mixes the track's
        # own audio (a silent background movie used to drop it entirely).
        hidden_length = 0.0
        for hidden_item in (main.get("clips") or []):
            if not isinstance(hidden_item, dict):
                continue
            source_in = max(0.0, _number(hidden_item.get("in"), 0.0))
            source_out = _number(hidden_item.get("out"), source_in + _number(hidden_item.get("duration"), 1.0))
            speed = max(0.25, min(4.0, _number(hidden_item.get("speed"), 1.0)))
            hidden_length = max(hidden_length, max(0.0, _number(hidden_item.get("start"), 0.0))
                                + max(0.001, (max(source_in + 0.001, source_out) - source_in) / speed))
        with tempfile.TemporaryDirectory(prefix="vam-render-") as temp_dir:
            base = os.path.join(temp_dir, "timeline-bg.mp4")
            result = ffmpeg_run(["-f", "lavfi", "-i", f"color=c={bg}:s={width}x{height}:r={fps}",
                                 "-t", f"{duration:.6f}", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                 base], 300)
            if result.returncode != 0 or not os.path.isfile(base):
                return False, "unable to render timeline"
            return _render_timeline_finish(base, output, document, slug, timeline, width, height, crf,
                                           temp_dir, max(duration, hidden_length), fps=fps, quality=quality,
                                           main_track_id=main_track_id)
    ratio = str((timeline.get("canvas") or {}).get("ratio") or preset.get("ratio") or "9:16")
    width, height = TIMELINE_RATIOS.get(ratio, (1080, 1920))
    canvas_background = str((timeline.get("canvas") or {}).get("background") or "#000000").lower()
    if not HEX_COLOR_RE.fullmatch(canvas_background):
        canvas_background = "#000000"
    fps = 30
    try:
        fps = int(preset.get("fps", timeline.get("fps", 30)))
    except (TypeError, ValueError, OverflowError):
        fps = 30
    if fps not in TIMELINE_FPS:
        fps = 30
    try:
        width = int(preset.get("width") or preset.get("w") or width)
        height = int(preset.get("height") or preset.get("h") or height)
        crf = int(preset.get("crf") or 20)
        quality = normalize_quality(preset.get("quality"))
    except (TypeError, ValueError, OverflowError):
        return False, "invalid render preset"
    if not (64 <= width <= 4096 and 64 <= height <= 4096 and 10 <= crf <= 45):
        return False, "invalid render preset"
    # Delivery target bitrate for the finished file (the parts carry it).
    target_kbps = export_bitrate_kbps(width, height, fps, quality)

    # The transition plan is needed before the parts are encoded: an outgoing
    # clip must carry a tail pad as long as its own blend, and the clip durations
    # are derivable from the items alone.
    def _planned_duration(item: dict) -> float:
        in_point = max(0.0, _number(item.get("in"), 0.0))
        out_point = _number(item.get("out"), in_point + _number(item.get("duration"), 1.0))
        speed = max(0.25, min(4.0, _number(item.get("speed"), 1.0)))
        return max(0.001, (out_point - in_point) / speed)

    planned_durations = [_planned_duration(item) for item in items]
    transitions: list[tuple[str, float, str]] = []
    for index in range(len(items) - 1):
        transition = items[index].get("transition_out") if isinstance(items[index], dict) else {}
        kind = str((transition or {}).get("kind") or "cut").lower()
        value = _number((transition or {}).get("duration"),
                        0.3 if kind in {"fade", "dip_black", "dip_white"} else 0.0)
        if kind not in TIMELINE_TRANSITIONS:
            kind = "cut"
        if kind in {"fade", "dip_black", "dip_white"}:
            value = min(2.0, max(0.001, value), planned_durations[index] / 2.0,
                        planned_durations[index + 1] / 2.0)
        else:
            value = 0.0
        easing = str((transition or {}).get("easing") or "linear").lower()
        if easing not in TIMELINE_EASINGS:
            easing = "linear"
        transitions.append((kind, value, easing))

    with tempfile.TemporaryDirectory(prefix="vam-render-") as temp_dir:
        normalized_files: list[str] = []
        durations: list[float] = []
        stream_lengths: list[float] = []
        for index, item in enumerate(items):
            source = _timeline_media_source(document, item, slug)
            if not source:
                return False, (
                    "timeline media is unavailable for "
                    f"{_timeline_item_label(item, index, main_track_id)}"
                )
            in_point = max(0.0, _number(item.get("in"), 0.0))
            out_point = _number(item.get("out"), in_point + _number(item.get("duration"), 1.0))
            speed = max(0.25, min(4.0, _number(item.get("speed"), 1.0)))
            duration = max(0.001, (out_point - in_point) / speed)
            destination = os.path.join(temp_dir, f"part-{index}.mp4")
            transform = item.get("transform") if isinstance(item.get("transform"), dict) else {}
            transform_scale = max(0.05, min(20.0, _number(transform.get("scale"), 1.0)))
            transform_rotate = _number(transform.get("rotate"), 0.0)
            vf_parts = []
            # Robust order: fit the source to the canvas, then apply the clip
            # transform (scale -> rotate -> center offset) so the export is
            # pixel-consistent with the live canvas compositor.
            vf_parts.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease")
            if abs(transform_scale - 1.0) > 1e-6:
                vf_parts.append(f"scale=iw*{transform_scale:.6f}:ih*{transform_scale:.6f}:force_original_aspect_ratio=decrease")
            if abs(transform_rotate) > 0.001:
                vf_parts.append(f"rotate={transform_rotate}*PI/180:fillcolor=black@0")
            opacity = max(0.05, min(1.0, _number(transform.get("opacity"), 1.0)))
            if opacity < 0.999:
                vf_parts.extend(["format=rgba", f"colorchannelmixer=aa={opacity:.3f}"])
            # A segment needs a cloned tail for its *incoming* blend as well: the
            # blend takes its first frames, so without the tail the last segment
            # of a timeline ends short of its own window.
            incoming_blend = transitions[index - 1][1] if index > 0 else 0.0
            outgoing_blend = transitions[index][1] if index < len(transitions) else 0.0
            tail_pad = min(2.5, max(incoming_blend, outgoing_blend) + 0.05) \
                if max(incoming_blend, outgoing_blend) > 0.0 else 0.0
            offset_x = _number(transform.get("x"), 0.0)
            offset_y = _number(transform.get("y"), 0.0)
            # Place the frame at (canvas centre + offset) and keep only the canvas
            # window.  ``pad`` alone cannot do that: a transform scale above 100%
            # (a zoom the live canvas simply clips) makes the frame larger than the
            # canvas, and pad refuses to shrink its input, so the encoder died with
            # "Padded dimensions cannot be smaller than input dimensions" and the
            # export surfaced "unable to encode timeline part N" while the preview
            # looked fine.  Pad out to a frame that also contains the shifted image,
            # then crop the canvas window back out.  When the frame is smaller than
            # the canvas the crop is a no-op and pad still supplies the background,
            # so scale <= 100% keeps its previous geometry.
            left = f"(({width}-iw)/2+({offset_x:.3f}))"
            top = f"(({height}-ih)/2+({offset_y:.3f}))"
            origin_x = f"max(0\\,-{left})"
            origin_y = f"max(0\\,-{top})"
            vf_parts.append(
                f"pad=w='{origin_x}+max({width}\\,{left}+iw)'"
                f":h='{origin_y}+max({height}\\,{top}+ih)'"
                f":x='{origin_x}+{left}':y='{origin_y}+{top}':color={canvas_background}"
            )
            vf_parts.append(f"crop={width}:{height}:{origin_x}:{origin_y}")
            vf_parts.extend([f"fps={fps}", "format=yuv420p", f"setpts=PTS/{speed:.6f}"])
            # A transition window sits flush against the outgoing clip's end, and
            # a chained xfade truncates the blend when the stream has no material
            # past the offset (a 0.2s dip reached only ~60% of black).  Cloning a
            # short tail keeps the whole curve; the chain is trimmed back below.
            if tail_pad > 0.0005:
                vf_parts.append(f"tpad=stop_mode=clone:stop_duration={tail_pad:.6f}")
            vf = ",".join(vf_parts)
            # Audio controls belong to the timeline item, not only to the
            # separate audio lane.  The previous renderer persisted gain and
            # fades but never applied them to a clip's own soundtrack, so
            # those controls appeared to work while exports stayed unchanged.
            detach_audio = bool(item.get("detach", False))
            # A tail pad longer than 0.5 ms has to cover the whole blend, and the
            # audio needs the same tail or acrossfade would end the soundtrack at
            # the outgoing cut.  `apad` deadlocks the simple-filter (`-af`) output
            # path on this build, so the pad is spliced in as a silent input.
            pad_seconds = tail_pad if tail_pad > 0.0005 else 0.0
            pad_input = (["-f", "lavfi", "-t", f"{pad_seconds:.6f}", "-i",
                          "anullsrc=channel_layout=stereo:sample_rate=44100"] if pad_seconds else [])
            has_audio = _probe_has_audio(source) and not detach_audio
            atempo = _atempo_chain(speed)
            try:
                if has_audio:
                    audio_filters = ["aresample=44100", "asetpts=PTS-STARTPTS"]
                    if atempo:
                        audio_filters.append(atempo)
                    gain = _number(item.get("gain"), 0.0)
                    if abs(gain) > 0.001:
                        audio_filters.append(f"volume={gain:.3f}dB")
                    fade_in = max(0.0, min(duration, _number(item.get("fade_in"), 0.0)))
                    fade_out = max(0.0, min(duration, _number(item.get("fade_out"), 0.0)))
                    if fade_in > 0.001:
                        audio_filters.append(f"afade=t=in:st=0:d={fade_in:.6f}")
                    if fade_out > 0.001:
                        audio_filters.append(f"afade=t=out:st={max(0.0, duration-fade_out):.6f}:d={fade_out:.6f}")
                    if pad_seconds:
                        graph = (f"[0:v]{vf}[vout];[0:a]{','.join(audio_filters)}[aclip];"
                                 f"[aclip][1:a]concat=n=2:v=0:a=1[aout]")
                        result = ffmpeg_run([
                            "-ss", f"{in_point:.6f}", "-t", f"{out_point - in_point:.6f}", "-i", source,
                        ] + pad_input + [
                            "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                            "-c:a", "aac", "-b:a", EXPORT_AUDIO_BITRATE,
                            "-ar", str(EXPORT_AUDIO_RATE), "-ac", "2",
                            "-movflags", "+faststart", destination,
                        ], 600)
                    else:
                        result = ffmpeg_run([
                            "-ss", f"{in_point:.6f}", "-t", f"{out_point - in_point:.6f}", "-i", source,
                            "-vf", vf, "-af", ",".join(audio_filters),
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                            "-c:a", "aac", "-b:a", EXPORT_AUDIO_BITRATE,
                            "-ar", str(EXPORT_AUDIO_RATE), "-ac", "2",
                            "-movflags", "+faststart", destination,
                        ], 600)
                else:
                    # Add deterministic silence so acrossfade can be used even
                    # when a generator returns a silent video.
                    result = ffmpeg_run([
                        "-ss", f"{in_point:.6f}", "-t", f"{out_point - in_point:.6f}", "-i", source,
                        "-f", "lavfi", "-t", f"{out_point - in_point + pad_seconds * speed:.6f}",
                        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                        "-map", "0:v:0", "-map", "1:a:0", "-vf", vf,
                        "-af", f"aresample=44100,asetpts=PTS-STARTPTS{(',' + atempo) if atempo else ''}",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                        "-c:a", "aac", "-b:a", EXPORT_AUDIO_BITRATE,
                        "-ar", str(EXPORT_AUDIO_RATE), "-ac", "2",
                        "-shortest", "-movflags", "+faststart", destination,
                    ], 600)
            except (OSError, subprocess.TimeoutExpired):
                return False, "render timed out"
            if result.returncode != 0 or not os.path.isfile(destination):
                return False, (
                    f"unable to encode timeline part {index + 1} "
                    f"({_timeline_item_label(item, index, main_track_id)}): "
                    f"{_encode_failure_reason(result.stderr)}"
                )
            normalized_files.append(destination)
            durations.append(duration)
            # xfade measures its inputs by real picture length, and a rendered
            # part's audio track can run a frame or two past its video.  Using the
            # nominal duration here made the first offset land beyond the gap
            # segment's picture, which collapsed the whole chain (VIDEO 1 froze
            # while overlays kept playing).
            measured_video = _probe_video_duration(destination)
            stream_lengths.append(measured_video if measured_video > 0
                                  else duration + pad_seconds)



        # The render window is the longest real timeline item, not just the
        # primary video track.  Audio, overlays, and captions live on
        # independent tracks; a soundtrack that outlives the last video frame
        # must therefore keep the output alive (the player will hold the last
        # video frame while audio continues).
        timeline_duration = max((max(0.0, _number(item.get("start"), 0.0)) + duration
                                 for item, duration in zip(items, durations)), default=0.01)
        for track_id in ("video-overlay", "video-2", "video-3", "audio-main"):
            if (tracks.get(track_id) or {}).get("hidden"):
                continue
            track_items = [item for item in ((tracks.get(track_id) or {}).get("clips") or [])
                           if isinstance(item, dict)]
            for item in track_items[:TIMELINE_MAX_ITEMS]:
                start = max(0.0, _number(item.get("start"), 0.0))
                source_in = max(0.0, _number(item.get("in"), 0.0))
                source_out = _number(item.get("out"), source_in + _number(item.get("duration"), 1.0))
                speed = max(0.25, min(4.0, _number(item.get("speed"), 1.0)))
                item_duration = max(0.001, (max(source_in + 0.001, source_out) - source_in) / speed)
                measured = _timeline_source_duration(document, item)
                if measured > source_in + 0.001 and (
                        not _number(item.get("source_duration"), 0.0)
                        or (track_id == "audio-main" and _number(item.get("source_duration"), 0.0) <= 1.01
                            and source_out <= source_in + 1.01)):
                    source_out = min(measured, max(source_in + 0.001, source_out))
                    item_duration = max(0.001, (source_out - source_in) / speed)
                timeline_duration = max(timeline_duration, start + item_duration)
        for cue in [cue for cue in ((tracks.get("text-main") or {}).get("cues") or []) if isinstance(cue, dict)]:
            timeline_duration = max(timeline_duration, max(0.0, _number(cue.get("end"), 0.0)))

        # Preserve intentional gaps created by moving clips on the timeline.
        # A gap is rendered as black video with silence; adjacent clips keep
        # the existing cut/fade path below.
        cursor = 0.0
        ordered_files: list[str] = []
        ordered_durations: list[float] = []
        ordered_transitions: list[tuple[str, float, str]] = []
        # Where each segment starts on the timeline, and the stream length it
        # really has on disk (nominal duration plus any cloned tail).
        ordered_starts: list[float] = []
        ordered_lengths: list[float] = []
        for index, (item, duration) in enumerate(zip(items, durations)):
            start = max(0.0, _number(item.get("start"), 0.0))
            gap = start - cursor
            # Keep the same seam tolerance as transition normalization. A
            # sub-50ms placement error is part of the cut window (and should
            # still flow through xfade/acrossfade); only a real intentional
            # gap materializes as black video and silence.
            if gap > 0.05:
                # gap -> clip is a hard cut; the clip keeps its own transition
                ordered_transitions.append(("cut", 0.0, "linear"))
                gap_file = os.path.join(temp_dir, f"gap-{index}.mp4")
                try:
                    gap_result = ffmpeg_run([
                        "-f", "lavfi", "-i", f"color=c={canvas_background}:s={width}x{height}:r={fps}",
                        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                        "-t", f"{gap:.6f}", "-shortest", "-c:v", "libx264", "-preset", "veryfast",
                        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-pix_fmt", "yuv420p", gap_file,
                    ], 300)
                except (OSError, subprocess.TimeoutExpired):
                    return False, "render timed out"
                if gap_result.returncode != 0 or not os.path.isfile(gap_file):
                    return False, "unable to create timeline gap"
                ordered_files.append(gap_file)
                ordered_durations.append(gap)
                ordered_starts.append(cursor)
                # The black segment's picture is a frame shorter than the gap it
                # stands for (a colour source at this frame rate rounds down).
                measured_gap = _probe_video_duration(gap_file)
                ordered_lengths.append(measured_gap if measured_gap > 0 else gap)
            ordered_files.append(normalized_files[index])
            ordered_durations.append(duration)
            ordered_starts.append(start)
            ordered_lengths.append(stream_lengths[index] if index < len(stream_lengths) else duration)
            # the last clip has no following boundary
            if index < len(transitions):
                ordered_transitions.append(transitions[index])
            cursor = max(cursor, start + duration)

        # The ordered sequence is the timeline order in every case; the gap pass
        # above only inserts the black segments for holes.
        normalized_files = ordered_files
        durations = ordered_durations
        transitions = ordered_transitions

        base_output = os.path.join(temp_dir, "timeline-base.mp4")
        if len(normalized_files) == 1:
            try:
                shutil.copyfile(normalized_files[0], base_output)
            except OSError:
                return False, "unable to write render output"
            return _render_timeline_finish(base_output, output, document, slug, timeline,
                                           width, height, crf, temp_dir, timeline_duration, fps=fps, quality=quality,
                                           main_track_id=main_track_id)

        if not any(value > 0 for _, value, _ in transitions):
            ok, message = concat_reencode(normalized_files, base_output, width=width, height=height, crf=crf, fps=fps,
                                bitrate_kbps=target_kbps)
            if not ok:
                return ok, message
            return _render_timeline_finish(base_output, output, document, slug, timeline,
                                           width, height, crf, temp_dir, timeline_duration, fps=fps, quality=quality,
                                           main_track_id=main_track_id)

        # Build a chained xfade/acrossfade graph. For a mixed cut/fade batch,
        # use a 1ms cross-dissolve for cut edges; this preserves ordering while
        # keeping the graph deterministic across FFmpeg versions.
        input_args: list[str] = []
        for path in normalized_files:
            input_args.extend(["-i", path])
        filters: list[str] = []
        video_label, audio_label = "0:v", "0:a"
        current_duration = ordered_lengths[0]
        for index in range(1, len(normalized_files)):
            transition_kind, transition_value, easing = transitions[index - 1]
            fade_duration = transition_value or 0.001
            # Place the blend at the timeline boundary instead of accumulating the
            # chain length.  Accumulating subtracted every blend from the running
            # total, so each clip after a fade slid earlier by the sum of the
            # blends and the movie came out shorter than the timeline.
            offset = max(0.0, ordered_starts[index] - fade_duration)
            # xfade clamps the window into its first input: when ``offset`` lands
            # at or past what the accumulated picture reaches, the transition
            # collapses and the movie ends early (measured: the output kept only
            # the first input).  It also rounds the offset to a frame, so a
            # sub-frame margin still fails — keep two frames of slack and clone
            # frames when the accumulator is short.
            margin = 2.0 / max(1, int(fps))
            needed = offset + fade_duration + margin
            if current_duration + 0.001 < needed:
                hold = needed - current_duration
                filters.append(f"[{video_label}]tpad=stop_mode=clone:stop_duration={hold:.6f}[{video_label}h]")
                video_label = f"{video_label}h"
                current_duration = needed
            next_video = f"v{index}"
            next_audio = f"a{index}"
            if easing == "linear" or transition_kind != "fade":
                # Built-in transitions: identical output for every project
                # that never sets an easing.  dip_black always stays built-in
                # (custom expressions cannot reach the per-plane black level).
                xfade_kind = {"dip_black": "fadeblack", "dip_white": "fadewhite"}.get(transition_kind, "fade")
                transition_filter = f"xfade=transition={xfade_kind}:duration={fade_duration:.6f}:offset={offset:.6f}"
            else:
                # Eased cross-dissolve: bake the easing curve into a custom
                # xfade expression.  The weights stay affine (A*(1-W)+B*W,
                # W in [0,1]) so per-plane evaluation is chroma-safe, and the
                # monitor mirrors the exact same math — preview and export
                # stay WYSIWYG.
                eased = TIMELINE_EASINGS[easing]
                expr = f"A*(1-({eased}))+B*({eased})"
                transition_filter = (
                    f"xfade=transition=custom:duration={fade_duration:.6f}"
                    f":offset={offset:.6f}:expr='{expr}'"
                )
            filters.append(f"[{video_label}][{index}:v]{transition_filter}[{next_video}]")
            filters.append(f"[{audio_label}][{index}:a]acrossfade=d={fade_duration:.6f}:c1=tri:c2=tri[{next_audio}]")
            video_label, audio_label = next_video, next_audio
            # xfade keeps the second input's own length (measured: offset + len).
            current_duration = offset + ordered_lengths[index]
        try:
            result = ffmpeg_run(input_args + [
                "-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-map", f"[{audio_label}]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                # Keep the movie exactly as long as the timeline: the cloned tails
                # that feed the blends must not extend it past the last clip.
                "-t", f"{max(0.01, timeline_duration):.6f}",
                "-movflags", "+faststart", base_output,
            ], 900)
        except (OSError, subprocess.TimeoutExpired):
            return False, "render timed out"
        if result.returncode != 0 or not os.path.isfile(base_output):
            return False, "unable to render timeline"
        return _render_timeline_finish(base_output, output, document, slug, timeline,
                                       width, height, crf, temp_dir, timeline_duration, fps=fps, quality=quality,
                                           main_track_id=main_track_id)


def _public_render_job(job: dict[str, object]) -> dict[str, object]:
    """Project a process-local render job to safe browser fields."""

    allowed = {"id", "status", "mode", "created", "updated", "file", "error", "rev"}
    return {key: value for key, value in job.items() if key in allowed}


def _render_job_update(job_id: str, **updates: object) -> None:
    with RENDER_LOCK:
        job = RENDER_JOBS.get(job_id)
        if not isinstance(job, dict):
            return
        job.update(updates)
        job["updated"] = now()


def _ensure_project_cover(slug: str, entry_path: str, document: dict) -> str | None:
    """Create one poster for a project that has none.

    A received or generated project never went through the upload poster step,
    so the shared index (and the home card) had no cover at all.  The poster is
    written once and then found on disk by later projections.
    """

    asset = document.get("asset") if isinstance(document.get("asset"), dict) else {}
    candidates: list[str] = []
    for item in (asset.get("finals") or []):
        if isinstance(item, dict):
            rel = norm_rel(item.get("file"))
            if rel:
                candidates.append(rel)
    for item in (asset.get("media") or []):
        if isinstance(item, dict) and str(item.get("kind") or "video") == "video":
            rel = norm_rel(item.get("file"))
            if rel:
                candidates.append(rel)
    for folder in ("clips", "media/proxies", "output"):
        base = os.path.join(entry_path, *folder.split("/"))
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if name.lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
                candidates.append(folder + "/" + name)
    if not candidates:
        return None
    poster_dir = os.path.join(entry_path, "media", "posters")
    try:
        os.makedirs(poster_dir, exist_ok=True)
    except OSError:
        return None
    digest = hashlib.sha256("|".join(candidates[:8]).encode("utf-8")).hexdigest()[:12]
    poster_name = "auto-" + digest + ".jpg"
    poster_path = os.path.join(poster_dir, poster_name)
    if os.path.isfile(poster_path):
        return "media/posters/" + poster_name
    for rel in candidates[:8]:
        source = safe_file(slug, rel)
        if source and safe_write_target(entry_path, poster_path) and make_poster(source, poster_path):
            return "media/posters/" + poster_name
    return None


def _probe_upload_ok(path: str) -> bool:
    """Confirm ffprobe can open the stored file as real media.

    Extension and MIME type are both client-controlled, so the stored bytes are
    the only trustworthy signal.
    """

    probe = shutil.which("ffprobe")
    if not probe:
        return True
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=format_name,duration",
             "-show_entries", "stream=codec_type", "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        # Without a usable ffprobe, fall back to the extension policy only.
        return True
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return False
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    return bool(str(fmt.get("format_name") or "").strip()) and bool(streams)


def _register_render_output(slug: str, output_rel: str, mode: str,
                            timeline: dict, job_id: str) -> None:
    """Append a public preview/export record without exposing worker details."""

    created = now()
    kind = "preview" if mode == "preview" else "export"
    final_id = stable_id("final", output_rel, kind, job_id)
    with LOCK:
        raw = read_json(project_file(slug), None)
        if not isinstance(raw, dict):
            return
        document = normalize_project(raw, slug)
        if not isinstance(document.get("asset"), dict):
            return
        finals = document["asset"].setdefault("finals", [])
        if any(isinstance(item, dict) and item.get("id") == final_id for item in finals):
            return
        main = next((track for track in timeline.get("tracks", [])
                     if isinstance(track, dict) and track.get("id") == "video-main"), {})
        source_order = [item.get("clip_id") for item in (main.get("clips", []) or [])
                        if isinstance(item, dict) and item.get("clip_id")]
        entry = {
            "id": final_id, "kind": kind, "file": output_rel,
            "name": "Studio preview" if kind == "preview" else "Studio export",
            "preset": None, "from": source_order, "created": created, "status": "active",
        }
        # Give the record a cover so the home card and the finals list can show
        # one; exports previously carried no poster at all.
        poster_rel = None
        try:
            poster_name = final_id + ".jpg"
            poster_target = os.path.join(project_dir(slug), "media", "posters", poster_name)
            source_file = safe_file(slug, output_rel)
            if (source_file and safe_write_target(project_dir(slug), poster_target)
                    and make_poster(source_file, poster_target)):
                poster_rel = "media/posters/" + poster_name
        except (OSError, ValueError):
            poster_rel = None
        if poster_rel:
            entry["poster"] = poster_rel
        if kind == "preview":
            # A preview render is an editor convenience, never a deliverable, so it
            # must not appear in the project's export list (it kept adding a
            # "Studio export" card every time a preview was rendered).
            for old in finals:
                if isinstance(old, dict) and old.get("kind") == "preview":
                    old["status"] = "superseded"
        else:
            finals.append(entry)
        finals[:] = _trim_preview_final_records(finals)
        document["asset"]["finals"] = finals
        document["assets"] = document["asset"].get("media", [])
        document["clips"] = document["asset"].get("clips", [])
        assembly = document.setdefault("assembly", {})
        if kind == "preview":
            assembly["preview"] = output_rel
        else:
            assembly.setdefault("exports", []).append({
                "file": output_rel, "preset": "Studio export", "created": created,
                "from": "timeline",
            })
        current_rev = document.get("rev", 0)
        try:
            current_rev = int(current_rev)
        except (TypeError, ValueError, OverflowError):
            current_rev = 0
        if kind == "preview":
            # A preview is derived data.  Advancing the revision here made every
            # edit race the commit that followed it (the commit arrived with a
            # stale revision and the server answered 409).
            document["rev"] = max(0, current_rev)
        else:
            document["rev"] = max(0, current_rev) + 1
        document["updated"] = created
        write_json_atomic(project_file(slug), document)
        # Only an explicit export is a user action worth recording.  A background
        # preview render fires on almost every edit, so logging it buried the real
        # operations under machine noise.
        if kind == "export":
            append_ops(slug, [{"op": "export.created", "file": output_rel, "preset": entry.get("name") or "Studio export"}],
                       document["rev"])
    sync_index()


RENDER_ARTIFACT_PREFIXES = ("studio-preview-r_", "studio-export-r_", "assembly-")
PREVIEW_FINALS_MAX = 4


def _trim_preview_final_records(finals: list[dict]) -> list[dict]:
    """Keep only the newest few ``preview`` records in a finals list.

    Preview renders are regenerable cache, not deliverables.  Without this
    cap a long editing session grew ``finals`` to dozens of superseded
    preview records (each pointing at a file that was never deleted).
    ``export`` records and the latest preview are never touched.
    """

    previews = [item for item in finals if isinstance(item, dict) and item.get("kind") == "preview"]
    if len(previews) <= PREVIEW_FINALS_MAX:
        return finals
    previews.sort(key=lambda item: str(item.get("created") or item.get("id") or ""), reverse=True)
    keep = {str(item.get("id")) for item in previews[:PREVIEW_FINALS_MAX]}
    return [item for item in finals
            if not (isinstance(item, dict) and item.get("kind") == "preview")
            or str(item.get("id")) in keep]


def _manifest_referenced_project_files(slug: str) -> set[str]:
    """Return every project-relative media path the manifest still references.

    Only ``final``/``assembly`` references are consulted: clip versions,
    proxies, and posters are owned by their own records and are never render
    artifacts.
    """

    raw = read_json(project_file(slug), None)
    if not isinstance(raw, dict):
        return set()
    document = normalize_project(raw, slug)
    referenced: set[str] = set()
    assembly = document.get("assembly") if isinstance(document.get("assembly"), dict) else {}
    for key in ("preview", "official"):
        value = assembly.get(key)
        normalized = norm_rel(value)
        if normalized:
            referenced.add(normalized)
    exports = assembly.get("exports")
    if isinstance(exports, list):
        for item in exports:
            if isinstance(item, dict):
                normalized = norm_rel(item.get("file"))
                if normalized:
                    referenced.add(normalized)
    finals = document.get("asset", {}).get("finals", []) if isinstance(document.get("asset"), dict) else []
    for item in (finals if isinstance(finals, list) else []):
        if isinstance(item, dict):
            normalized = norm_rel(item.get("file"))
            if normalized:
                referenced.add(normalized)
    return referenced


def _cleanup_render_output(output: str, part_output: str) -> None:
    """Remove an interrupted render's partial/final file without following links."""

    for candidate in (part_output, output):
        try:
            if candidate and os.path.isfile(candidate) and not os.path.islink(candidate):
                os.unlink(candidate)
        except OSError:
            pass


def _prune_render_artifacts(slug: str, *, keep_referenced: bool = True) -> None:
    """Delete superseded/abandoned render artifacts for one project.

    Every auto preview render used to create a brand-new
    ``studio-preview-r_<id>.mp4`` and only mark the older ``final`` record as
    superseded, so a heavily edited project accumulated dozens of preview
    copies (and partial 48-byte files after an interrupted render).  Files
    still referenced by ``final`` records, ``assembly.preview`` or
    ``assembly.exports`` are kept; everything else under the render artifact
    prefixes is removed, together with leftover ``.part``/temp files.
    """

    directory = project_dir(slug)
    if not path_within(ROOT, directory):
        return
    if keep_referenced:
        # Cap the preview record history before computing references so the
        # oldest cache files become unreferenced and are deleted below.  The
        # revision is preserved: this is bookkeeping cleanup, not an edit.
        raw = read_json(project_file(slug), None)
        if isinstance(raw, dict):
            try:
                document = normalize_project(raw, slug)
                finals = document.get("asset", {}).get("finals", [])
                if isinstance(finals, list):
                    trimmed = _trim_preview_final_records(finals)
                    if len(trimmed) != len(finals):
                        document["asset"]["finals"] = trimmed
                        document["assets"] = document["asset"].get("media", [])
                        document["clips"] = document["asset"].get("clips", [])
                        write_json_atomic(project_file(slug), document)
                        sync_index()
            except Exception:
                pass
    referenced = _manifest_referenced_project_files(slug) if keep_referenced else set()
    for subdir in ("media/proxies", "media/output/exports", "media/output"):
        base_dir = os.path.join(directory, subdir.replace("/", os.sep))
        try:
            entries = os.listdir(base_dir) if os.path.isdir(base_dir) else []
        except OSError:
            continue
        for name in entries:
            candidate = os.path.join(base_dir, name)
            rel = os.path.relpath(candidate, directory).replace(os.sep, "/")
            try:
                if not os.path.isfile(candidate) or os.path.islink(candidate):
                    continue
                if not path_within(directory, candidate):
                    continue
                if name.startswith(RENDER_ARTIFACT_PREFIXES) and not (keep_referenced and rel in referenced):
                    os.unlink(candidate)
                elif name.endswith(".part") or name.startswith(".tmp."):
                    os.unlink(candidate)
            except OSError:
                continue


def _sweep_orphan_render_artifacts() -> None:
    """Startup sweep: remove render leftovers from restarts and crashed jobs.

    The manager restarts (Capafy restart or ``ensure.py --upgrade``) can kill
    a render thread mid-ffmpeg; the previous code left the truncated output at
    its final path and the job vanished with the process.  This sweep deletes
    any render artifact the current manifests no longer reference.
    """

    for slug in (entry for entry in sorted(os.listdir(ROOT))
                 if not is_reserved_slug(entry) and SLUG_RE.fullmatch(entry)):
        try:
            _prune_render_artifacts(slug)
        except Exception:
            continue


def _run_render_job(job_id: str, slug: str, document: dict, timeline: dict,
                    output: str, mode: str, preset: dict, revision: int) -> None:
    _render_job_update(job_id, status="running")
    # Render into a sibling temp name that still ends in .mp4: ffmpeg infers
    # the muxer from the file extension, and a ".part" suffix made every
    # compositor/finalization pass fail with "Unable to choose an output
    # format".  The atomic rename below keeps an interrupted render from
    # leaving a truncated file that later looks like a completed preview.
    if output.lower().endswith(".mp4"):
        part_output = output[:-4] + ".tmp.mp4"
    else:
        part_output = output + ".tmp.mp4"
    try:
        os.makedirs(os.path.dirname(output), exist_ok=True)
        ok, message = _render_timeline_file(document, slug, timeline, part_output, preset)
        if not ok:
            _cleanup_render_output(output, part_output)
            _render_job_update(job_id, status="failed", error=message or "render failed")
            return
        # Promote atomically: an interrupted render can never leave a
        # truncated file that later looks like a completed preview.
        os.replace(part_output, output)
        output_rel = os.path.relpath(output, project_dir(slug)).replace(os.sep, "/")
        _register_render_output(slug, output_rel, mode, timeline, job_id)
        # The chosen quality belongs to the project, so the next dialog opens on the
        # user's own choice.  Only the "nothing to composite" copy path used to call
        # this, which is why a normal export never remembered anything.
        if mode == "export":
            save_export_quality(slug, preset.get("quality"))
        _render_job_update(job_id, status="completed", file=output_rel, rev=revision)
        # A successful render supersedes older previews; delete their files so
        # a long editing session does not accumulate hundreds of copies.
        try:
            _prune_render_artifacts(slug)
        except Exception:
            pass
    except Exception:
        # Do not put exception text, commands, absolute paths or provider
        # details in the browser-visible job. The worker is fail-closed and
        # the generator-neutral manager remains available for further edits.
        _cleanup_render_output(output, part_output)
        _render_job_update(job_id, status="failed", error="render failed")


_COMPRESSIBLE_TYPES = frozenset({
    "text/html", "text/css", "text/plain", "text/javascript", "text/xml",
    "application/javascript", "application/x-javascript", "application/json",
    "application/xml", "image/svg+xml", "application/manifest+json",
    "font/ttf", "font/otf", "font/woff", "font/woff2", "application/font-sfnt",
    "application/x-font-ttf", "application/vnd.ms-fontobject",
})

# Some runtimes do not map every font/text extension in ``mimetypes``, so the
# suffix list is the authoritative check.
_COMPRESSIBLE_SUFFIXES = frozenset({
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".txt", ".xml",
    ".webmanifest", ".ttf", ".otf", ".woff", ".woff2", ".eot",
})

_STATIC_GZIP_CACHE: dict[tuple[str, int, int], bytes] = {}


def _gzip_static_asset(path: str) -> tuple[bytes, float] | None:
    """Return ``(gzipped_bytes, mtime)`` for a compressible static asset.

    The editor bundle is roughly 570 KB of uncompressed HTML/CSS/JS that is
    re-sent on every load, so compressing it is the largest transfer win.
    Results are cached by path + mtime + size, keeping the work off the request
    path once a file has been served.
    """

    try:
        info = os.stat(path)
    except OSError:
        return None
    key = (path, int(info.st_mtime_ns), int(info.st_size))
    cached = _STATIC_GZIP_CACHE.get(key)
    if cached is None:
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        cached = gzip.compress(raw, compresslevel=5)
        if len(_STATIC_GZIP_CACHE) > 24:
            _STATIC_GZIP_CACHE.clear()
        _STATIC_GZIP_CACHE[key] = cached
    return cached, info.st_mtime


class _RangeReader:
    """Bound a file object to one byte range for the stdlib copyfile loop.

    ``SimpleHTTPRequestHandler`` copies the returned object wholesale, so a
    partial response needs a reader that stops at the end of the range.
    """

    def __init__(self, handle, length: int) -> None:
        self._handle = handle
        self._remaining = max(0, int(length))

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size is None or size < 0 or size > self._remaining:
            size = self._remaining
        data = self._handle.read(size)
        self._remaining -= len(data)
        return data

    def readinto(self, buffer) -> int:  # pragma: no cover - depends on stdlib
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass

    def __getattr__(self, name):
        return getattr(self._handle, name)


class Handler(SimpleHTTPRequestHandler):
    server_version = "VideoAssetManager/2"
    # Keep the management window's frequent API requests on one TCP connection.
    # The JSON and static responses provide Content-Length, so HTTP/1.1 is safe
    # here and avoids repeated localhost connection setup during drag edits.
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    timeout = 30

    def handle_one_request(self) -> None:
        # A persistent connection reuses the handler, not just the socket.
        # Path/range/body state must belong to exactly one request.
        self._vam_path_stripped = False
        self._vam_accept_ranges = False
        self._vam_body_consumed = False
        self._head_only = False
        return super().handle_one_request()

    def log_message(self, fmt: str, *args: object) -> None:
        # Do not log request bodies, paths containing user data, or credentials.
        return

    @staticmethod
    def _translate_from_root(path: str) -> str:
        """Translate a URL path against ``ROOT``, never the process CWD.

        ``SimpleHTTPRequestHandler.translate_path`` deliberately uses
        ``os.getcwd()``.  The manager may be launched from any host directory,
        while its data and bundled page live under ``ROOT``.  Relying on the
        stdlib implementation would make static resolution depend on the
        caller's working directory.
        Keep the URL normalization semantics, but make the serving root
        explicit so process ownership and file resolution are independent.
        """
        try:
            raw = unquote(str(path or "").split("?", 1)[0].split("#", 1)[0])
        except (TypeError, ValueError):
            raw = "/"
        trailing_slash = raw.rstrip().endswith("/")
        normalized = posixpath.normpath(raw)
        words = [word for word in normalized.split("/") if word]
        candidate = os.path.abspath(ROOT)
        for word in words:
            # Match SimpleHTTPRequestHandler's traversal filtering.  The
            # request-level guards perform the stronger device/link checks.
            if word in (os.curdir, os.pardir) or os.path.dirname(word):
                continue
            candidate = os.path.join(candidate, word)
        if trailing_slash:
            candidate += os.sep
        return candidate

    def translate_path(self, path: str) -> str:
        """Keep static files inside the manager root, including symlink checks.

        The stdlib handler normalizes ``..`` and follows links while opening a
        file. Resolve and validate the candidate before it reaches that
        handler; ``send_head`` repeats the check immediately before opening to
        cover a link replacement race.
        """
        if getattr(self, "_vam_path_stripped", False):
            matched, app_path, query = True, canonical_request_path(path), ""
            parsed = urlparse(str(path or ""))
            query = parsed.query or ""
        else:
            matched, app_path, query = application_request_path(path)
        if not matched:
            return os.path.join(ROOT, ".vam-not-found")
        # SimpleHTTPRequestHandler resolves against ``self.path``.  Use the
        # stripped application path while retaining the query for its normal
        # filename semantics.
        translated = self._translate_from_root(app_path + (("?" + query) if query else ""))
        if not self._static_target_allowed(app_path, translated):
            return os.path.join(ROOT, ".vam-not-found")
        return translated

    def _json(self, code: int, payload: object, etag: str | None = None) -> None:
        # Every JSON response is customer-visible, including conflict/error
        # payloads that may include a manifest supplied by the browser.  Apply
        # the same privacy projection here as on normal project reads.
        # ``etag`` gives a revalidating response: a client that already holds this
        # revision gets a 304 instead of the whole document again.
        token = '"%s"' % etag if etag else None
        if token and code == 200:
            try:
                if str(self.headers.get("If-None-Match") or "").strip() == token:
                    self.send_response(304)
                    self.send_header("ETag", token)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            except AttributeError:
                pass
        safe_payload = sanitize_public(payload)
        if safe_payload is _DROP:
            safe_payload = {"error": "request failed"}
        try:
            body = json.dumps(safe_payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            body = b'{"error":"request failed"}'
        # Project manifests and list rows are large and polled frequently;
        # gzip them when the client accepts it (same-origin management window).
        compressed_value = False
        if len(body) >= 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            try:
                compressed = gzip.compress(body, compresslevel=5)
                if len(compressed) < len(body):
                    body = compressed
                    compressed_value = True
            except (OSError, ValueError):
                pass
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache" if token else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if token:
            self.send_header("ETag", token)
        if compressed_value:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(body)

    def _read_body(self, limit: int = 20 * 1024 * 1024) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            return None  # This server accepts bounded Content-Length bodies.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        body = self.rfile.read(length)
        self._vam_body_consumed = len(body) == length
        return body if self._vam_body_consumed else None

    def _body_json(self, limit: int = 20 * 1024 * 1024) -> object:
        body = self._read_body(limit)
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _project_exists(self, slug: str) -> bool:
        if not SLUG_RE.match(slug) or is_reserved_slug(slug):
            return False
        project_path = os.path.abspath(project_dir(slug))
        if os.path.islink(project_path) or not os.path.isdir(project_path) or not path_within(ROOT, project_path):
            return False
        manifest = os.path.abspath(project_file(slug))
        # Do not follow a manifest symlink, even when it points back inside the
        # project.  Atomic writes replace the manifest itself and therefore
        # remain safe after this check.
        return not os.path.islink(manifest) and os.path.isfile(manifest) and path_within(project_path, manifest)

    @staticmethod
    def _private_static_path(path: str) -> bool:
        """Whether a URL path names a private/control-plane resource."""

        canonical = canonical_request_path(path)
        if "\x00" in canonical:
            return True
        if not canonical.startswith("/"):
            canonical = "/" + canonical
        segments = [part for part in canonical.split("/") if part not in ("", ".")]
        lower_segments = [part.casefold() for part in segments]
        # Reject traversal even though the stdlib handler would normalize it;
        # this also catches double-encoded traversal in the guard itself.
        devices = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if any(part == ".." or ":" in part or any(ord(ch) < 0x20 for ch in part)
               or part.upper().split(".", 1)[0] in devices for part in segments):
            return True
        # Any dot-prefixed component is transient/private (runtime metadata,
        # handoff markers, temporary writes, and Python caches).
        if any(part.startswith(".") for part in segments):
            return True
        # Internal queues and archived projects are never browser resources;
        # check every component so a symlink alias cannot hide one below a
        # seemingly public project path.
        if any(part in PRIVATE_STATIC_SEGMENTS for part in lower_segments):
            return True
        # Manifests, recoverable state, root projections, and launcher metadata
        # are served only through their sanitized API/launcher contracts.
        if any(part in PRIVATE_STATIC_FILENAMES for part in lower_segments):
            return True
        if not lower_segments:
            return False
        first = lower_segments[0]
        if first == "api":
            return True
        if first == "webapp":
            # The bundled page is intentionally reachable only through the
            # root alias. Runtime metadata and source files stay private.
            if len(lower_segments) <= 1:
                return True
            leaf = lower_segments[-1]
            if "__pycache__" in lower_segments or leaf not in PUBLIC_WEBAPP_FILES:
                return True
            return False
        # No root-level files other than the webapp alias are public. A valid
        # project path must name one of the documented resource directories;
        # this prevents accidental exposure of ad-hoc files and directory
        # listings (including a project manifest under an encoded alias).
        if not SLUG_RE.fullmatch(segments[0]) or is_reserved_slug(segments[0]):
            return True
        if len(lower_segments) < 2:
            return True
        project_area = lower_segments[1]
        if project_area not in PUBLIC_PROJECT_DIRS:
            return True
        if project_area == "assets":
            if len(lower_segments) < 3 or lower_segments[2] not in PUBLIC_ASSET_DIRS:
                return True
        elif project_area == "logs":
            if len(lower_segments) != 3 or lower_segments[2] not in PUBLIC_LOG_FILES:
                return True
        return False

    def _static_target_allowed(self, request_path: str,
                               translated: str | None = None) -> bool:
        """Validate a static request by URL and resolved filesystem target."""

        if self._private_static_path(request_path):
            return False
        try:
            candidate = translated if translated is not None else self._translate_from_root(request_path)
            root_abs = os.path.abspath(ROOT)
            candidate_abs = os.path.abspath(candidate)
            root_real = os.path.realpath(root_abs)
            candidate_real = os.path.realpath(candidate_abs)
            if not path_within(root_real, candidate_real):
                return False
            # Directory listings are not part of the manager contract and can
            # reveal private filenames. Only concrete files are public.
            if os.path.isdir(candidate_abs):
                return False
            # Do not follow a symlink/junction even when it points back inside
            # the manager root; otherwise a public alias could target inbox,
            # staging, or another hidden file after the URL check.
            if path_contains_link(root_abs, candidate_abs):
                return False
            try:
                resolved_rel = os.path.relpath(candidate_real, root_real).replace(os.sep, "/")
            except (OSError, ValueError):
                return False
            if self._private_static_path("/" + resolved_rel):
                return False
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def send_head(self):  # type: ignore[override]
        """Apply private/symlink checks immediately before static file open."""

        if getattr(self, "_vam_path_stripped", False):
            parsed = urlparse(self.path)
            matched, request_path, query = True, canonical_request_path(parsed.path), parsed.query or ""
        else:
            matched, request_path, query = application_request_path(self.path)
        if not matched:
            self._head_not_found()
            return None
        if self._private_static_path(request_path):
            self._head_not_found()
            return None
        try:
            translated = self._translate_from_root(request_path + (("?" + query) if query else ""))
        except Exception:
            self._head_not_found()
            return None
        if not self._static_target_allowed(request_path, translated):
            self._head_not_found()
            return None
        # Public text assets can contain arbitrary generator output. Refuse a
        # file wholesale when it contains a remote reference, absolute path,
        # credential-shaped field, or other internal routing text instead of
        # allowing the stdlib handler to stream it verbatim.
        try:
            if (not request_path.casefold().startswith("/webapp/")
                    and os.path.isfile(translated)
                    and os.path.splitext(translated)[1].lower() in PUBLIC_TEXT_EXTENSIONS):
                with open(translated, "rb") as handle:
                    if os.fstat(handle.fileno()).st_size > 4 * 1024 * 1024:
                        self._head_not_found()
                        return None
                    sample = handle.read(4 * 1024 * 1024 + 1).decode("utf-8", "ignore")
                if _looks_private(sample):
                    self._head_not_found()
                    return None
        except OSError:
            self._head_not_found()
            return None
        # Media elements only allow seeking when the response advertises byte
        # ranges.  The stdlib handler has no range support at all, so a local
        # mp4 was unseekable and the playhead scrub showed a frozen frame.
        try:
            size = os.path.getsize(translated)
        except OSError:
            size = None
        if size is not None:
            self._vam_accept_ranges = True
            try:
                span = self._requested_range(size)
            except (TypeError, ValueError):
                span = None
            if span == "unsatisfiable":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if span:
                start, end = span
                length = end - start + 1
                try:
                    handle = open(translated, "rb")
                except OSError:
                    self._vam_accept_ranges = False
                    self._head_not_found()
                    return None
                handle.seek(start)
                self.send_response(206)
                self.send_header("Content-Type", self.guess_type(translated))
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return _RangeReader(handle, length)
            if not span:
                compressed = self._gzip_text_asset(translated)
                if compressed is not None:
                    body, mtime = compressed
                    if self._not_modified_since(mtime):
                        self.send_response(304)
                        self.send_header("Last-Modified", self.date_time_string(mtime))
                        self._vam_accept_ranges = False
                        self.end_headers()
                        return None
                    self.send_response(200)
                    self.send_header("Content-Type", self.guess_type(translated))
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Last-Modified", self.date_time_string(mtime))
                    self._vam_accept_ranges = False
                    self.end_headers()
                    return io.BytesIO(body)
        try:
            return super().send_head()
        finally:
            self._vam_accept_ranges = False

    def _not_modified_since(self, mtime: float) -> bool:
        """True when the client's If-Modified-Since covers this file's mtime."""

        raw = (self.headers.get("If-Modified-Since") or "").strip()
        if not raw:
            return False
        try:
            since = self.date_time_string(mtime)
        except (TypeError, ValueError, OverflowError):
            return False
        return raw == since

    def _gzip_text_asset(self, path: str):
        """Compress text assets when the client accepts gzip and sends no range."""

        if "gzip" not in (self.headers.get("Accept-Encoding") or "").lower():
            return None
        if self.headers.get("Range"):
            return None
        kind = (self.guess_type(path) or "").split(";", 1)[0].strip().lower()
        suffix = os.path.splitext(path)[1].lower()
        if kind not in _COMPRESSIBLE_TYPES and suffix not in _COMPRESSIBLE_SUFFIXES:
            return None
        try:
            if os.path.getsize(path) < 1024:
                return None
        except OSError:
            return None
        return _gzip_static_asset(path)

    def _requested_range(self, size: int):
        """Return (start, end) for a single-range request, or None/"unsatisfiable"."""

        header = (self.headers.get("Range") or "").strip()
        if not header.lower().startswith("bytes="):
            return None
        spec = header.split("=", 1)[1].split(",", 1)[0].strip()
        start_text, _, end_text = spec.partition("-")
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            if not end_text:
                return None
            start = max(0, size - int(end_text))
            end = size - 1
        if size <= 0 or start >= size or end < start:
            return "unsatisfiable"
        return start, min(end, size - 1)

    def end_headers(self):
        if getattr(self, "_vam_accept_ranges", False):
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def end_headers(self) -> None:  # type: ignore[override]
        """Stop browsers from heuristically caching the bundled editor bundle.

        The stdlib static handler sends only ``Last-Modified``, which lets a
        browser reuse a heuristically-fresh copy without revalidating.  That
        made editor updates invisible until a manual hard refresh, so the
        bundled page/scripts/stylesheets are always revalidated.  Media files
        and project assets keep their normal caching behaviour.
        """

        # Early errors and bodyless actions can reply before consuming a POST
        # body. Close that connection so leftover bytes cannot become the next
        # request. The next valid edit transparently opens a fresh connection.
        headers = getattr(self, "headers", {})
        if (not getattr(self, "_vam_body_consumed", False)
                and (headers.get("Transfer-Encoding")
                     or headers.get("Content-Length", "0") != "0")):
            self.close_connection = True
            self.send_header("Connection", "close")
        raw_path = str(getattr(self, "path", "") or "")
        path, _, query = raw_path.partition("?")
        path = path.casefold()
        if path.startswith("/webapp/") and path.endswith((".html", ".js", ".css")):
            if "v=" in query:
                # The bundle is referenced with ?v=<build>, so a new build is a
                # new URL: cache it hard and never revalidate it.
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            else:
                # Unversioned editor text still revalidates, but a 304 replaces
                # re-sending the whole file.
                self.send_header("Cache-Control", "no-cache")
        if getattr(self, "_vam_accept_ranges", False):
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def _head_not_found(self) -> None:
        self.send_response(404)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _public_log_request(self, path: str) -> tuple[bool, str | None]:
        """Resolve a project log request without following links.

        ``logs/*.log`` is the one text resource the page reads directly. Keep
        it available for the UI, but route it through a sanitized projection;
        malformed paths are treated as a handled 404 rather than falling back
        to ``SimpleHTTPRequestHandler``.
        """

        canonical = canonical_request_path(path)
        parts = [part for part in canonical.split("/") if part not in ("", ".")]
        if len(parts) != 3 or parts[1].casefold() != "logs":
            return False, None
        if parts[2].casefold() not in PUBLIC_LOG_FILES:
            return True, None
        slug = parts[0]
        if not SLUG_RE.fullmatch(slug) or is_reserved_slug(slug) or not self._project_exists(slug):
            return True, None
        try:
            manifest = read_json(project_file(slug), None)
            if not isinstance(manifest, dict):
                return True, None
            document = normalize_project(manifest, slug)
            valid, _ = validate_project(document, slug)
            if not valid:
                return True, None
        except (OSError, TypeError, ValueError):
            return True, None
        target = os.path.abspath(os.path.join(project_dir(slug), "logs", parts[2]))
        try:
            # Reject a link at any component, including a junction replacing
            # the logs directory or the log file itself.
            if (os.path.islink(target) or not os.path.isfile(target)
                    or not path_within(project_dir(slug), target)
                    or path_contains_link(project_dir(slug), target)):
                return True, None
        except OSError:
            return True, None
        return True, target

    @staticmethod
    def _sanitize_log_bytes(target: str) -> bytes:
        """Return a bounded, line-oriented public projection of a log file."""

        max_bytes = 4 * 1024 * 1024
        try:
            with open(target, "rb") as handle:
                try:
                    size = os.fstat(handle.fileno()).st_size
                except OSError:
                    size = max_bytes
                if size > max_bytes:
                    handle.seek(max(0, size - max_bytes))
                raw = handle.read(max_bytes)
        except OSError:
            return b""
        text = raw.decode("utf-8", "ignore")
        # If we started in the middle of a line, discard the partial record.
        if size > max_bytes and "\n" in text:
            text = text.split("\n", 1)[1]
        lines: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except (TypeError, ValueError):
                clean_text = _privacy_sanitize_public_text(
                    line, limit=16_000, fallback=None, allow_public_url=False
                )
                if clean_text is None or _looks_private(clean_text):
                    continue
                clean_line = clean_text
            else:
                clean_value = sanitize_public(parsed)
                if clean_value is _DROP:
                    continue
                try:
                    clean_line = json.dumps(clean_value, ensure_ascii=False, separators=(",", ":"))
                except (TypeError, ValueError):
                    continue
            lines.append(clean_line)
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    def _serve_public_log(self, path: str, *, head: bool = False) -> bool:
        """Serve a sanitized project log, returning whether the path matched."""

        matched, target = self._public_log_request(path)
        if not matched:
            return False
        if target is None:
            self._head_not_found()
            return True
        body = self._sanitize_log_bytes(target)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)
        return True

    def do_HEAD(self) -> None:
        """Apply the same private-file guard as GET before static HEAD serving."""
        matched, path, query = application_request_path(self.path)
        if not matched:
            return self._head_not_found()
        if path in ("/", "/index.html"):
            self.path = "/webapp/index.html"
            path = "/webapp/index.html"
        else:
            self.path = path + (("?" + query) if query else "")
        self._vam_path_stripped = True
        if self._private_static_path(path):
            return self._head_not_found()
        if self._serve_public_log(path, head=True):
            return
        return super().do_HEAD()

    def _serve_project_download(self, slug: str, rel: str, inline: bool | None = None) -> bool:
        """Serve a project file through the API so a prefix-only host can fetch it.

        Hosted previews proxy ``/api/...`` but not arbitrary project paths, so a
        plain media URL there answers with the host's own HTML error page.  This
        streams the same file with Range support and an attachment name.
        """

        if not SLUG_RE.fullmatch(slug):
            return False
        clean = norm_rel(rel)
        if not clean or ".." in clean.replace("\\", "/").split("/"):
            self._json(400, {"error": "invalid file"})
            return True
        target = safe_file(slug, clean)
        if not target or not os.path.isfile(target):
            self._json(404, {"error": "file not found"})
            return True
        try:
            size = os.path.getsize(target)
        except OSError:
            self._json(416, {"error": "file unavailable"})
            return True

        start_byte, end_byte, status = 0, max(0, size - 1), 200
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", str(self.headers.get("Range") or "").strip())
        if match and size:
            first, last = match.group(1), match.group(2)
            if first:
                start_byte = int(first)
                end_byte = int(last) if last else size - 1
            elif last:
                start_byte = max(0, size - int(last))
            if start_byte >= size or start_byte > end_byte:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            status = 206
        end_byte = min(end_byte, size - 1)
        length = max(0, end_byte - start_byte + 1)

        try:
            handle = open(target, "rb")
        except OSError:
            self._json(416, {"error": "file unavailable"})
            return True
        name = os.path.basename(target).replace('"', "")
        try:
            handle.seek(start_byte)
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(target)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            # ``inline`` may be forced by the caller (the query-free file route).
            inline_ok = bool(inline) if inline is not None else str(
                (parse_qs(urlparse(self.path).query).get("inline") or [""])[0]).strip() == "1"
            disposition = "inline" if inline_ok else "attachment"
            self.send_header("Content-Disposition", f'{disposition}; filename="{name}"')
            # Derived media is addressed by content or job id (posters, filmstrips,
            # proxies, clip versions, finished exports), so the browser may reuse it
            # without a revalidation round trip.  Uploaded assets keep revalidating
            # because a re-upload can reuse the same name.  A Studio entry used to
            # re-request a dozen thumbnails on every switch.
            if clean.replace("\\", "/").startswith(
                    ("media/posters/", "media/filmstrips/", "media/proxies/",
                     "clips/", "output/exports/")):
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            else:
                self.send_header("Cache-Control", "no-cache")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{size}")
            self.end_headers()
            if not getattr(self, "_head_only", False):
                shutil.copyfileobj(_RangeReader(handle, length), self.wfile, 256 * 1024)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            handle.close()
        return True

    def do_HEAD(self) -> None:
        """Answer HEAD for the API download route, then fall back to static HEAD."""

        matched, path, _query = application_request_path(self.path)
        if matched:
            match = re.match(r"^/api/project/([^/]+)/download$", path)
            if match:
                params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self._head_only = True
                if self._serve_project_download(match.group(1), (params.get("file") or [""])[0]):
                    return
            match = re.match(r"^/api/project/([^/]+)/file/(.+)$", path)
            if match:
                self._head_only = True
                if self._serve_project_download(match.group(1), urllib.parse.unquote(match.group(2)), inline=True):
                    return
        super().do_HEAD()

    # Browsers ask for this on every page load; answering with a real (tiny)

    # icon keeps the console free of a spurious 404.

    FAVICON = base64.b64decode(

            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/wD/AL0AAAAASUVORK5CYII=")

    def do_GET(self) -> None:
        matched, path, query = application_request_path(self.path)
        if matched and path == "/favicon.ico":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.FAVICON)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(self.FAVICON)
            return
        if not matched:
            return self._json(404, {"error": "not found"})
        lower_path = path.lower()
        if path == "/api/health":
            return self._json(200, {"ok": True, "ffmpeg": has_ffmpeg(), "service": RUNTIME_ID,
                                    "runtime_id": RUNTIME_ID, "pid": os.getpid(),
                                    "port": self.server.server_address[1],
                                    "sync": sync_status_snapshot()})
        if path == "/api/projects/changes":
            # The list poll only needs a token and aggregate scanner state;
            # fetch the full project rows after the token changes.
            index = read_index_cached()
            return self._json(200, {
                "index_token": index_change_token(index),
                "sync": sync_status_snapshot(),
            })
        if path == "/api/library":
            library = workspace_library()
            # The index token identifies the exact revision of this payload, so a
            # client that already holds it can be answered with an empty 304
            # instead of 21 KB every time the surface is revisited.
            return self._json(200, library, etag=str(library.get("index_token") or ""))
        if path == "/api/projects":
            schedule_task_sync()
            # ``read_index_cached`` performs only a bounded manifest signature
            # probe and rebuilds the projection when needed; do not recursively
            # walk every project on each list poll.  It owns the writer lock
            # only for the occasional rebuild, so concurrent stable reads are
            # not serialized behind a directory probe.
            index = read_index_cached()
            if isinstance(index, dict):
                index = dict(index)
                index["index_token"] = index_change_token(index)
                sync = sync_status_snapshot()
                index["pending_count"] = int(sync.get("pending_count", pending_handoff_count()) or 0)
                index["unmatched_count"] = int(sync.get("unmatched_count", 0) or 0)
                index["delivery_failed_count"] = int(sync.get("delivery_failed_count", 0) or 0)
                index["sync"] = sync
                return self._json(200, index)
            sync = sync_status_snapshot()
            return self._json(200, {
                "schema": 1, "projects": [], "index_token": index_change_token({}),
                "pending_count": int(sync.get("pending_count", pending_handoff_count()) or 0),
                "unmatched_count": int(sync.get("unmatched_count", 0) or 0),
                "delivery_failed_count": int(sync.get("delivery_failed_count", 0) or 0),
                "sync": sync,
            })
        match = re.match(r"^/api/project/([^/]+)/changes$", path)
        if match:
            slug = match.group(1)
            if not self._project_exists(slug):
                return self._json(404, {"error": "no such project"})
            with LOCK:
                snapshot = project_change_snapshot(slug)
            if snapshot is None:
                return self._json(404, {"error": "no such project"})
            snapshot["sync"] = sync_status_snapshot()
            return self._json(200, snapshot)
        match = re.match(r"^/api/project/([^/]+)/timeline$", path)
        if match:
            slug = match.group(1)
            if not SLUG_RE.fullmatch(slug) or is_reserved_slug(slug):
                return self._json(400, {"error": "bad slug"})
            if not self._project_exists(slug):
                return self._json(404, {"error": "no such project"})
            return self._timeline_get(slug)
        if path == "/api/logs":
            try:
                wanted = int((parse_qs(query).get("limit") or ["200"])[0])
            except (TypeError, ValueError):
                wanted = 200
            limit = max(1, min(500, wanted))
            entries: list[dict] = []
            index = read_index_cached()
            rows = index.get("projects") if isinstance(index, dict) else None
            for item in (rows if isinstance(rows, list) else []):
                entry = str((item or {}).get("slug") or "") if isinstance(item, dict) else ""
                if not entry or not SLUG_RE.fullmatch(entry):
                    continue
                log_path = os.path.join(project_dir(entry), "logs", "edits.log")
                if not os.path.isfile(log_path) or os.path.islink(log_path):
                    continue
                try:
                    with open(log_path, "rb") as handle:
                        handle.seek(0, os.SEEK_END)
                        size = handle.tell()
                        handle.seek(max(0, size - 256 * 1024))
                        tail = handle.read().decode("utf-8", "replace").splitlines()
                except OSError:
                    continue
                for line in tail[-limit:]:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        # Never trust the file: historical lines were written
                        # before the current rules existed.
                        safe = sanitize_log_entry(row)
                        if safe.get("op"):
                            safe["project"] = entry
                            entries.append(safe)
            entries.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
            return self._json(200, {"ok": True, "entries": entries[:limit]})
        match = re.match(r"^/api/project/([^/]+)/download$", path)
        if match:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if self._serve_project_download(match.group(1), (query.get("file") or [""])[0]):
                return
        # Query-free source address for a project file: a plain path any host can
        # proxy, served inline so it can also be opened in a browser.
        match = re.match(r"^/api/project/([^/]+)/file/(.+)$", path)
        if match:
            if self._serve_project_download(match.group(1), urllib.parse.unquote(match.group(2)), inline=True):
                return
        match = re.match(r"^/api/project/([^/]+)/render/([^/]+)$", path)
        if match:
            slug, job_id = match.group(1), match.group(2)
            if not SLUG_RE.fullmatch(slug) or is_reserved_slug(slug):
                return self._json(400, {"error": "bad slug"})
            if not self._project_exists(slug):
                return self._json(404, {"error": "no such project"})
            return self._render_get(slug, job_id)
        match = re.match(r"^/api/project/([^/]+)$", path)
        if match:
            schedule_task_sync()
            slug = match.group(1)
            if not self._project_exists(slug):
                return self._json(404, {"error": "no such project", "sync": sync_status_snapshot()})
            with LOCK:
                raw = read_json(project_file(slug), {})
                state_path = os.path.join(project_dir(slug), "state.json")
                # State is recoverable bookkeeping, but it is still returned
                # to the browser.  Refuse symlink/junction targets so a
                # workspace link cannot expose an external JSON document.
                state = {}
                if (not os.path.islink(state_path)
                        and os.path.isfile(state_path)
                        and path_within(project_dir(slug), state_path)):
                    state = read_json(state_path, {})
                changes = project_change_snapshot(slug)
            # The snapshot is detached from disk. Normalization, privacy
            # filtering and socket writes must not serialize other readers or
            # editor commits behind a slow client.
            document = normalize_project(raw, slug)
            valid, error = validate_project(document, slug)
            if not valid:
                return self._json(409, {"error": error or "project requires repair"})
            return self._json(200, {
                "project": document,
                "state": state,
                "changes": changes,
                "sync": sync_status_snapshot(),
            })
        match = re.match(r"^/api/project/([^/]+)/zip$", path)
        if match:
            return self._zip(match.group(1))
        if lower_path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if self._serve_public_log(path):
            return
        if path in ("/", "/index.html"):
            self.path = "/webapp/index.html"
        elif self._private_static_path(path):  # never expose transient/private files
            return self._json(404, {"error": "not found"})
        else:
            self.path = path + (("?" + query) if query else "")
        self._vam_path_stripped = True
        return super().do_GET()

    def _projects_from_index(self) -> list[dict]:
        index = read_index_cached()
        return index.get("projects", []) if isinstance(index, dict) and isinstance(index.get("projects"), list) else []

    @staticmethod
    def _revision(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and re.fullmatch(r"\d{1,18}", value.strip()):
            try:
                parsed = int(value.strip())
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
        return None

    def _timeline_response(self, slug: str, document: dict, *, status: int = 200,
                           changes: dict | None = None) -> None:
        projection = timeline_projection(document)
        try:
            revision = int(document.get("rev", 0))
        except (TypeError, ValueError, OverflowError):
            revision = 0
        if changes is None:
            changes = project_change_snapshot(slug)
        self._json(status, {
            "ok": status < 400,
            "project": document,
            "timeline": projection["timeline"],
            "order": projection["order"],
            "rev": revision,
            "changes": changes if isinstance(changes, dict) else None,
        })

    def _timeline_get(self, slug: str) -> None:
        with LOCK:
            raw = read_json(project_file(slug), None)
            changes = project_change_snapshot(slug)
        if not isinstance(raw, dict):
            return self._json(409, {"error": "project manifest is invalid"})
        document = normalize_project(raw, slug)
        valid, error = validate_project(document, slug)
        if not valid:
            return self._json(409, {"error": error or "project requires repair"})
        return self._timeline_response(slug, document, changes=changes)

    def _timeline_commit(self, slug: str) -> None:
        payload = self._body_json(2 * 1024 * 1024)
        if not isinstance(payload, dict):
            return self._json(400, {"error": "bad request"})
        base_rev = self._revision(payload.get("base_rev"))
        if base_rev is None:
            return self._json(400, {"error": "bad revision"})
        operations = payload.get("operations")
        if operations is None and isinstance(payload.get("ops"), list):
            operations = payload.get("ops")
        with LOCK:
            raw = read_json(project_file(slug), None)
            if not isinstance(raw, dict):
                return self._json(409, {"error": "project manifest is invalid"})
            document = normalize_project(raw, slug)
            valid, error = validate_project(document, slug)
            if not valid:
                return self._json(409, {"error": error or "project requires repair"})
            current_rev = self._revision(document.get("rev", 0))
            if current_rev is None:
                return self._json(409, {"error": "invalid current revision"})
            current_projection = timeline_projection(document)
            if current_rev != base_rev:
                return self._json(409, {
                    "error": "rev conflict", "project": sanitize_public(document),
                    "timeline": sanitize_public(current_projection["timeline"]),
                    "order": current_projection["order"], "rev": current_rev,
                })
            # Undo/redo is process-local by design: it never serializes hidden
            # editor snapshots into a project or ZIP.  A revision mismatch
            # invalidates the stack, preventing an old browser from replaying
            # edits after another writer changed the project.  Keep the
            # invalidation fence separate from individual snapshots: every
            # undo/redo increments ``project.rev``, but older snapshots must
            # remain available for consecutive multi-step undo/redo.
            history_key, history_state, redo_state = _timeline_history_stacks(slug, current_rev)
            op_names = [str(item.get("op") or item.get("type") or "").lower()
                        for item in operations] if isinstance(operations, list) else []
            next_history = list(history_state)
            next_redo = list(redo_state)
            if any(name in {"undo", "redo"} for name in op_names):
                if len(op_names) != 1 or op_names[0] not in {"undo", "redo"}:
                    return self._json(400, {"error": "undo/redo must be committed separately"})
                stack = history_state if op_names[0] == "undo" else redo_state
                if not stack:
                    return self._json(409, {"error": "no compatible undo/redo state"})
                target_entry = stack[-1]
                target = target_entry.get("timeline") if isinstance(target_entry, dict) else None
                if not isinstance(target, dict):
                    return self._json(409, {"error": "undo/redo state is unavailable"})
                if op_names[0] == "undo":
                    next_history = list(history_state[:-1])
                    next_redo = list(redo_state)
                    next_redo.append({"timeline": copy.deepcopy(current_projection["timeline"])})
                else:
                    next_redo = list(redo_state[:-1])
                    next_history = list(history_state)
                    next_history.append({"timeline": copy.deepcopy(current_projection["timeline"])})
                next_history = next_history[-TIMELINE_MAX_HISTORY:]
                next_redo = next_redo[-TIMELINE_MAX_HISTORY:]
                updated_timeline, error = copy.deepcopy(target), None
            else:
                updated_timeline, error = apply_timeline_operations(
                    document, current_projection["timeline"], operations
                )
                if updated_timeline is not None:
                    next_history = (list(history_state) + [{
                        "timeline": copy.deepcopy(current_projection["timeline"]),
                    }])[-TIMELINE_MAX_HISTORY:]
                    next_redo = []
            if updated_timeline is None:
                return self._json(400, {"error": error or "invalid timeline operation"})
            valid, error = validate_timeline(document, updated_timeline)
            if not valid:
                return self._json(400, {"error": error or "invalid timeline"})
            assembly = document.setdefault("assembly", {})
            assembly["timeline"] = updated_timeline
            updated_main = next((track for track in updated_timeline.get("tracks", [])
                                 if isinstance(track, dict) and track.get("id") == "video-main"), {})
            assembly["order"] = [item.get("clip_id") for item in (updated_main.get("clips", []) or [])
                                  if isinstance(item, dict) and item.get("clip_id")]
            # Keep the legacy audio projection synchronized for old clients.
            if isinstance(updated_timeline.get("audio"), dict):
                assembly["audio"] = copy.deepcopy(updated_timeline["audio"])
            document["rev"] = current_rev + 1
            document["updated"] = now()
            valid, error = validate_project(document, slug)
            if not valid:
                return self._json(400, {"error": error or "invalid project"})
            write_json_atomic(project_file(slug), document)
            append_ops(slug, operations, document["rev"])
            # Publish the new editor history only after the manifest write has
            # succeeded.  A rejected/failed commit therefore leaves undo/redo
            # exactly as it was before the request.
            history_state[:] = next_history[-TIMELINE_MAX_HISTORY:]
            redo_state[:] = next_redo[-TIMELINE_MAX_HISTORY:]
            TIMELINE_HISTORY_HEAD[history_key] = document["rev"]
        # Publish the current project's row before responding; unchanged
        # projects need no normalization, poster work, or recursive size walk.
        # The independent watcher still receives generator handoffs. An editor
        # drag is not itself a request to start that scanner.
        sync_index(timeline_slug=slug)
        return self._timeline_response(slug, document)

    def _render_get(self, slug: str, job_id: str) -> None:
        if not ID_RE.fullmatch(job_id):
            return self._json(400, {"error": "bad render job id"})
        with RENDER_LOCK:
            job = RENDER_JOBS.get(job_id)
            if not isinstance(job, dict) or job.get("slug") != slug:
                return self._json(404, {"error": "render job not found"})
            return self._json(200, _public_render_job(job))

    def _render_post(self, slug: str) -> None:
        payload = self._body_json(1 * 1024 * 1024)
        if not isinstance(payload, dict):
            payload = {}
        mode = str(payload.get("mode") or "preview").lower()
        if mode not in {"preview", "export"}:
            return self._json(400, {"error": "invalid render mode"})
        preset = payload.get("preset") if isinstance(payload.get("preset"), dict) else {}
        with LOCK:
            raw = read_json(project_file(slug), None)
            if not isinstance(raw, dict):
                return self._json(409, {"error": "project manifest is invalid"})
            document = normalize_project(raw, slug)
            valid, error = validate_project(document, slug)
            if not valid:
                return self._json(409, {"error": error or "project requires repair"})
            projection = timeline_projection(document)
            timeline = projection["timeline"]
            current_rev = self._revision(document.get("rev", 0))
            if current_rev is None:
                return self._json(409, {"error": "invalid current revision"})
            requested_rev = payload.get("timeline_rev")
            if requested_rev is not None:
                requested_rev = self._revision(requested_rev)
                if requested_rev is None:
                    return self._json(400, {"error": "bad timeline revision"})
                if requested_rev != current_rev:
                    return self._json(409, {
                        "error": "rev conflict", "project": sanitize_public(document),
                        "timeline": sanitize_public(timeline), "rev": current_rev,
                    })
            # Report every unusable item up front, naming the lane, the clip, and
            # its start time, instead of letting the encoder discover the first
            # one after a full render.  Only the lane that becomes the main
            # picture can actually fail the render; other lanes are reported as
            # warnings and still honoured the way they always were.
            preflight = _timeline_preflight(document, timeline, slug)
            blocking, warnings = preflight["blocking"], preflight["warnings"]
            if blocking:
                summary = "; ".join(blocking[:4])
                if len(blocking) > 4:
                    summary += "; and %d more" % (len(blocking) - 4)
                return self._json(422, {
                    "error": "this timeline is not ready to render: " + summary,
                    "problems": blocking[:TIMELINE_MAX_ITEMS],
                    "warnings": warnings[:TIMELINE_MAX_ITEMS],
                })
            job_id = "r_" + uuid.uuid4().hex
            directory = os.path.join(project_dir(slug), "media", "proxies" if mode == "preview" else "output", "exports" if mode == "export" else "")
            # Avoid an empty path component while keeping all output locations
            # project-relative and covered by the normal boundary checks.
            directory = os.path.normpath(directory)
            suffix = "preview" if mode == "preview" else "export"
            output = os.path.join(directory, f"studio-{suffix}-{job_id}.mp4")
            if not safe_write_target(project_dir(slug), output):
                return self._json(400, {"error": "invalid render output path"})
            job = {
                "id": job_id, "slug": slug, "mode": mode, "status": "queued",
                "created": now(), "updated": now(), "rev": current_rev,
            }
            with RENDER_LOCK:
                RENDER_JOBS[job_id] = job
                if len(RENDER_JOBS) > RENDER_MAX_JOBS:
                    stale = sorted(RENDER_JOBS.items(), key=lambda pair: str(pair[1].get("updated") or ""))[:-RENDER_MAX_JOBS]
                    for stale_id, _ in stale:
                        RENDER_JOBS.pop(stale_id, None)
        thread = threading.Thread(
            target=_run_render_job,
            args=(job_id, slug, document, timeline, output, mode, preset, current_rev),
            name=f"vam-render-{job_id}", daemon=True,
        )
        thread.start()
        return self._json(202, {"ok": True, "job_id": job_id, "status": "queued"})

    def _zip(self, slug: str) -> None:
        if not self._project_exists(slug):
            return self._json(404, {"error": "no such project"})
        source_root = os.path.realpath(project_dir(slug))
        fd, temporary = tempfile.mkstemp(prefix="vam-", suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for base, dirs, files in os.walk(source_root):
                    safe_dirs: list[str] = []
                    for name in dirs:
                        if name.lower() in {"staging", "__pycache__"} or name.startswith("."):
                            continue
                        directory = os.path.join(base, name)
                        relative_dir = os.path.relpath(directory, source_root).replace(os.sep, "/")
                        relative_parts = [part.casefold() for part in relative_dir.split("/") if part]
                        if (os.path.islink(directory)
                                or path_contains_link(source_root, directory)
                                or any(part.startswith(".") or part in PRIVATE_STATIC_SEGMENTS
                                       for part in relative_parts)):
                            continue
                        safe_dirs.append(name)
                    dirs[:] = safe_dirs
                    for name in files:
                        if name.startswith(".") or name.lower() in {"runtime.json", "server.log"}:
                            continue
                        lexical = os.path.join(base, name)
                        relative_lexical = os.path.relpath(lexical, source_root).replace(os.sep, "/")
                        relative_parts = [part.casefold() for part in relative_lexical.split("/") if part]
                        if (os.path.islink(lexical)
                                or path_contains_link(source_root, lexical)
                                or any(part.startswith(".") or part in PRIVATE_STATIC_SEGMENTS
                                       for part in relative_parts)):
                            continue
                        absolute = os.path.realpath(lexical)
                        if not path_within(source_root, absolute):
                            continue
                        relative = os.path.relpath(absolute, source_root).replace(os.sep, "/")
                        relative_parts = [part.casefold() for part in relative.split("/") if part]
                        if any(part.startswith(".") or part in PRIVATE_STATIC_SEGMENTS for part in relative_parts):
                            continue
                        relative_lower = relative.lower()
                        if "diagnostic" in relative_lower:
                            # Diagnostics are internal by definition, even if
                            # a caller happened to scrub their contents.
                            continue
                        if "task_" in relative_lower and relative_lower.startswith("tasks/"):
                            # Task summaries are allowed only when they are
                            # valid public JSON without private fields.
                            if not name.lower().endswith(".json"):
                                continue
                            data = read_json(absolute, None)
                            blob = json.dumps(data, ensure_ascii=False) if data is not None else ""
                            if PRIVATE_RE.search(blob) or ABSOLUTE_RE.search(blob):
                                continue
                        # Public text/JSON resources are allowed only when
                        # they do not contain obvious private fields or
                        # absolute paths. Binary media is copied untouched.
                        if os.path.splitext(name)[1].lower() in {".json", ".jsonl", ".md", ".txt", ".csv"}:
                            # Text resources are copied verbatim into the
                            # portable ZIP, so inspect their complete content
                            # before inclusion.  Sampling only the first few
                            # megabytes could leave a signed URL or absolute
                            # path at the end of a large log/report.  Skip
                            # oversized text rather than risk leaking it.
                            try:
                                if os.path.getsize(absolute) > 4 * 1024 * 1024:
                                    continue
                                with open(absolute, "rb") as handle:
                                    sample = handle.read(4 * 1024 * 1024 + 1).decode("utf-8", "ignore")
                            except OSError:
                                continue
                            if _looks_private(sample):
                                continue
                        archive.write(absolute, relative)
            size = os.path.getsize(temporary)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{slug}.zip"')
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with open(temporary, "rb") as handle:
                shutil.copyfileobj(handle, self.wfile)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def do_POST(self) -> None:
        matched, path, query = application_request_path(self.path)
        if not matched:
            return self._json(404, {"error": "not found"})
        if path == "/api/sync":
            # A manual scan is still idempotent and generator-neutral.  The
            # watcher may already be running; ``force`` only bypasses the
            # throttle for this one request.
            schedule_task_sync(force=True)
            return self._json(200, {"ok": True, "sync": sync_status_snapshot()})
        if path == "/api/projects/create":
            return self._create()
        if path.startswith("/api/project/") and path.endswith("/import-asset"):
            slug = path[len("/api/project/"):-len("/import-asset")].strip("/")
            payload = self._body_json(1 * 1024 * 1024)
            try:
                return self._json(200, workspace_import_asset(slug, payload if isinstance(payload, dict) else {}))
            except WorkspaceRequestError as exc:
                return self._json(exc.status, exc.payload)
        if path.startswith("/api/project/") and path.endswith("/media/prepare"):
            slug = path[len("/api/project/"):-len("/media/prepare")].strip("/")
            payload = self._body_json(1 * 1024 * 1024)
            try:
                return self._json(200, workspace_prepare_media(slug, payload if isinstance(payload, dict) else {}))
            except WorkspaceRequestError as exc:
                return self._json(exc.status, exc.payload)
        match = re.match(r"^/api/project/([^/]+)/(save|upload|assemble|trim|export|timeline/commit|render|adopt-first-video)$", path)
        if not match:
            return self._json(404, {"error": "not found"})
        slug, action = match.group(1), match.group(2)
        if not SLUG_RE.match(slug) or is_reserved_slug(slug):
            return self._json(400, {"error": "bad slug"})
        if action == "adopt-first-video":
            return self._adopt_first_video(slug)
        if not self._project_exists(slug):
            return self._json(404, {"error": "no such project"})
        if action == "timeline/commit":
            return self._timeline_commit(slug)
        if action == "render":
            return self._render_post(slug)
        return getattr(self, "_" + action)(slug, parse_qs(query))

    def _create(self) -> None:
        payload = self._body_json(1 * 1024 * 1024)
        payload = payload if isinstance(payload, dict) else {}
        title = str(payload.get("title") or "未命名项目").strip()[:80]
        if _looks_private(title):
            return self._json(400, {"error": "title contains private data"})
        ptype = payload.get("type") if payload.get("type") in ("clone", "generate") else "clone"
        with LOCK:
            # Generate the slug while holding the process-wide lock.  Without
            # this, two simultaneous creates could choose the same path and
            # overwrite one another's manifest.
            base = re.sub(r"[^A-Za-z0-9\-]+", "-", title).strip("-")[:40] or "project"
            slug = f"{time.strftime('%Y-%m-%d')}-{base}"
            count = 0
            while os.path.lexists(project_dir(slug)) and count < 1000:
                count += 1
                slug = f"{time.strftime('%Y-%m-%d')}-{base}-{count}"
            if not SLUG_RE.match(slug) or is_reserved_slug(slug):
                return self._json(500, {"error": "slug generation failed"})
            # A pre-existing symlink/junction must never become a project
            # boundary.  Create the directory atomically and refuse an
            # unexpected race instead of following it.
            try:
                os.makedirs(project_dir(slug), exist_ok=False)
            except FileExistsError:
                return self._json(409, {"error": "project already exists"})
            for rel in (
                "assets/uploads", "assets/generated", "assets/scripts", "assets/reports",
                "source/segments", "clips", "output/exports", "media/posters",
                "media/proxies", "media/filmstrips", "media/waveforms", "subtitles",
                "staging/generator", "tasks", "logs",
            ):
                os.makedirs(os.path.join(project_dir(slug), rel), exist_ok=True)
            document = empty_project(slug, title, ptype)
            write_json_atomic(project_file(slug), document)
            write_json_atomic(os.path.join(project_dir(slug), "state.json"), {
                "schema": 1, "rev": 0, "phase": "draft", "active_clip": None,
                "queue": [], "last_error": {"clip": None, "message": None}, "updated": now(),
            })
            write_text_atomic(os.path.join(ROOT, "active_project"), slug)
            append_ops(slug, [{"op": "project.created", "title": title, "ts": now()}], 0)
            sync_index()
            schedule_task_sync(force=True)
        return self._json(200, {"ok": True, "slug": slug})

    def _save(self, slug: str, query: dict[str, list[str]]) -> None:
        payload = self._body_json()
        if not isinstance(payload, dict) or not isinstance(payload.get("doc"), dict):
            return self._json(400, {"error": "bad request"})
        unknown = set(payload["doc"]) - SAVE_TOP_LEVEL_KEYS
        if unknown:
            # The save endpoint is a public write boundary.  Keep the reader
            # compatible with legacy/extension fields, but do not allow a
            # browser payload to persist arbitrary top-level data.
            return self._json(400, {"error": "unsupported project fields"})
        raw_base_rev = payload.get("base_rev")
        # JSON booleans are Python ints, and ``int(1.5)`` silently truncates;
        # neither is a valid optimistic-concurrency revision.  Accept a
        # non-negative integer or its decimal string representation only.
        if isinstance(raw_base_rev, bool):
            return self._json(400, {"error": "bad revision"})
        if isinstance(raw_base_rev, int):
            base_rev = raw_base_rev
        elif isinstance(raw_base_rev, str) and len(raw_base_rev.strip()) <= 18 and re.fullmatch(r"\d+", raw_base_rev.strip()):
            try:
                base_rev = int(raw_base_rev.strip())
            except ValueError:
                return self._json(400, {"error": "bad revision"})
        else:
            return self._json(400, {"error": "bad revision"})
        if base_rev < 0:
            return self._json(400, {"error": "bad revision"})
        document = normalize_project(payload["doc"], slug)
        document["slug"] = slug
        ok, error = validate_project(document, slug)
        if not ok:
            return self._json(400, {"error": error or "invalid project"})
        with LOCK:
            current_raw = read_json(project_file(slug), empty_project(slug))
            current = normalize_project(current_raw, slug)
            raw_current_rev = current.get("rev", 0)
            if isinstance(raw_current_rev, bool):
                return self._json(409, {"error": "invalid current revision"})
            try:
                current_rev = int(raw_current_rev)
            except (TypeError, ValueError, OverflowError):
                return self._json(409, {"error": "invalid current revision"})
            if current_rev < 0:
                return self._json(409, {"error": "invalid current revision"})
            if current_rev != base_rev:
                return self._json(409, {"error": "rev conflict", "doc": current})
            document["rev"] = current_rev + 1
            document["updated"] = now()
            write_json_atomic(project_file(slug), document)
            append_ops(slug, payload.get("ops"), document["rev"])
        sync_index()
        schedule_task_sync(force=True)
        return self._json(200, {"ok": True, "rev": document["rev"]})

    def _upload(self, slug: str, query: dict[str, list[str]]) -> None:
        body = self._read_body(UPLOAD_MAX_BODY + 1024 * 1024)
        if body is None:
            return self._json(400, {"error": "empty or oversized body"})
        project_path = os.path.abspath(project_dir(slug))
        if os.path.islink(project_path) or not path_within(ROOT, project_path):
            return self._json(400, {"error": "invalid project boundary"})
        with LOCK:
            assets_root = os.path.join(project_path, "assets")
            directory = os.path.join(project_path, "assets", "uploads")
            # Validate every existing parent before creating anything.  A
            # symlink/junction such as ``assets -> outside`` must not be
            # followed by os.makedirs, even briefly, because that would make
            # an upload write outside the project boundary.
            for parent in (assets_root, directory):
                if os.path.lexists(parent):
                    if (os.path.islink(parent) or not os.path.isdir(parent)
                            or not path_within(project_path, parent)):
                        return self._json(400, {"error": "invalid upload directory"})
                else:
                    os.makedirs(parent, exist_ok=True)
                if os.path.islink(parent) or not path_within(project_path, parent):
                    return self._json(400, {"error": "invalid upload directory"})
            if not os.path.isdir(directory):
                return self._json(400, {"error": "invalid upload directory"})
            original = (query.get("name") or ["upload.bin"])[0]
            name = os.path.basename(original or "upload.bin")
            name = re.sub(r"[^\w.\-一-鿿]+", "_", name)[:120] or "upload.bin"
            if name in {".", ".."}:
                name = "upload.bin"
            stem, suffix = os.path.splitext(name)
            candidate, index = name, 0
            # ``lexists`` also catches dangling links.  Never open a path that
            # was pre-created as a symlink, even if its target is absent.
            while os.path.lexists(os.path.join(directory, candidate)):
                index += 1
                candidate = f"{stem}-{index}{suffix}"
            name = candidate
            destination = os.path.join(directory, name)
            if not path_within(project_path, destination):
                return self._json(400, {"error": "invalid upload path"})

            # Type and size policy, enforced before a single byte is stored.
            upload_ext = suffix.lower()
            if upload_ext in UPLOAD_REJECT_EXT:
                return self._json(400, {"error": f"{upload_ext} files are not supported. {UPLOAD_HELP}"})
            upload_kind = ("image" if upload_ext in IMG_EXT else "video" if upload_ext in VID_EXT
                           else "audio" if upload_ext in AUD_EXT else None)
            if upload_kind is None:
                return self._json(400, {"error": f"Unsupported file type {upload_ext or '(none)'}. {UPLOAD_HELP}"})
            upload_limit = UPLOAD_LIMITS[upload_kind]
            if len(body) > upload_limit:
                return self._json(400, {"error": f"{upload_kind} files must be {upload_limit // (1024 * 1024)} MB or smaller."})
            # A legitimate image can be well under 1 KB, so only reject bytes
            # that cannot be any media at all.
            if len(body) < 64:
                return self._json(400, {"error": "The uploaded file is empty or truncated."})

            # Read and validate the current manifest before writing bytes.  A
            # malformed/private manifest must not leave an orphaned upload or
            # trigger an uncaught ``int(rev)`` error after the file is saved.
            current_raw = read_json(project_file(slug), None)
            if not isinstance(current_raw, dict):
                return self._json(409, {"error": "project manifest is invalid"})
            document = normalize_project(current_raw, slug)
            valid, error = validate_project(document, slug)
            if not valid:
                return self._json(409, {"error": error or "project manifest is invalid"})
            raw_current_rev = document.get("rev", 0)
            if isinstance(raw_current_rev, bool):
                return self._json(409, {"error": "invalid current revision"})
            try:
                current_rev = int(raw_current_rev)
            except (TypeError, ValueError, OverflowError):
                return self._json(409, {"error": "invalid current revision"})
            if current_rev < 0:
                return self._json(409, {"error": "invalid current revision"})

            try:
                # Exclusive creation prevents a concurrent request (or a
                # newly-created symlink) from redirecting an overwrite after
                # the lexists/path checks above.
                with open(destination, "xb") as handle:
                    handle.write(body)
            except FileExistsError:
                return self._json(409, {"error": "upload path became unavailable"})
            if not _probe_upload_ok(destination):
                # A renamed non-media file (for example a document saved as .mp4)
                # must not enter the library, so verify the real container.
                try:
                    os.unlink(destination)
                except OSError:
                    pass
                return self._json(400, {"error": f"The file is not readable as {upload_kind} media. {UPLOAD_HELP}"})
            rel = os.path.relpath(destination, project_path).replace(os.sep, "/")
            extension = suffix.lower()
            kind = "image" if extension in IMG_EXT else "video" if extension in VID_EXT else "audio" if extension in AUD_EXT else "text"
            poster = rel if kind == "image" else None
            if kind == "video" and has_ffmpeg():
                poster_path = os.path.join(project_path, "media", "posters", f"upload-{hashlib.sha256(rel.encode()).hexdigest()[:12]}.jpg")
                if safe_write_target(project_path, poster_path) and make_poster(destination, poster_path):
                    poster = os.path.relpath(poster_path, project_path).replace(os.sep, "/")
            digestor = hashlib.sha256()
            with open(destination, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digestor.update(chunk)
            digest = digestor.hexdigest()
            asset_id = stable_id("as", rel, digest)
            media = document.setdefault("asset", {}).setdefault("media", [])
            existing = next((item for item in media if item.get("id") == asset_id), None)
            if existing is None:
                media.append({
                    "id": asset_id, "kind": kind, "file": rel, "name": name,
                    "origin": "upload", "group": None, "tags": [],
                    "poster": poster, "used_by": [], "status": "active",
                    "hash": "sha256:" + digest,
                })
                document["assets"] = media
                document["rev"] = current_rev + 1
                document["updated"] = now()
                write_json_atomic(project_file(slug), document)
                append_ops(slug, [{"op": "asset.upload", "id": asset_id, "name": name, "path": rel, "ts": document["updated"]}], document["rev"])
                sync_index()
            else:
                # A repeated upload with the same stable id is a no-op at the
                # manifest layer; the file name collision logic above still
                # keeps distinct bytes available to the user.
                poster = existing.get("poster") or poster
            current_rev = int(document.get("rev", current_rev))
        return self._json(200, {"ok": True, "id": asset_id, "path": rel, "poster": poster, "kind": kind, "name": name, "hash": "sha256:" + digest, "rev": current_rev})

    def _assemble(self, slug: str, query: dict[str, list[str]]) -> None:
        if not has_ffmpeg():
            return self._json(501, {"error": "ffmpeg unavailable"})
        payload = self._body_json(1 * 1024 * 1024)
        ids = payload.get("ids") if isinstance(payload, dict) else None
        if not isinstance(ids, list) or not 1 <= len(ids) <= 50:
            return self._json(400, {"error": "bad request"})
        # Resolve the legacy order from one validated snapshot.  Rendering is
        # intentionally outside ``LOCK`` so a slow ffmpeg operation cannot
        # block ordinary project reads or timeline edits; the manifest is
        # re-read and materialized atomically after the file is complete.
        with LOCK:
            raw = read_json(project_file(slug), None)
            if not isinstance(raw, dict):
                return self._json(409, {"error": "project manifest is invalid"})
            document = normalize_project(raw, slug)
            valid, error = validate_project(document, slug)
            if not valid:
                return self._json(409, {"error": error or "project requires repair"})
            normalized_ids: list[str] = []
            sources: list[str] = []
            for value in ids:
                clip_id = str(value or "").strip()
                if not clip_id or clip_id not in _clip_lookup(document):
                    return self._json(400, {"error": "unknown clip in assembly order"})
                rel = clip_media(document, clip_id, True)
                source = safe_file(slug, rel)
                if not source:
                    return self._json(400, {"error": "clip media is unavailable"})
                normalized_ids.append(clip_id)
                sources.append(source)
        output = os.path.join(project_dir(slug), "media", "proxies", f"assembly-{time.time_ns()}.mp4")
        if not safe_write_target(project_dir(slug), output):
            return self._json(400, {"error": "invalid assembly output path"})
        ok, message = concat_reencode(sources, output)
        if not ok:
            return self._json(500, {"error": message})
        rel = os.path.relpath(output, project_dir(slug)).replace(os.sep, "/")

        # The old endpoint used to return a path only, leaving the generated
        # preview orphaned until the browser happened to issue a separate
        # ``/save`` request.  Materialize the result here as a normal public
        # final and keep the schema-1 order projection in lockstep with the
        # canonical primary timeline.  If the project changed while ffmpeg was
        # running, use the latest manifest but retain the explicitly requested
        # order; this route is intentionally a compatibility adapter and has no
        # optimistic-concurrency input of its own.
        materialized = False
        materialized_rev: int | None = None
        final_id = stable_id("final", rel, "preview")
        try:
            with LOCK:
                latest_raw = read_json(project_file(slug), None)
                if not isinstance(latest_raw, dict):
                    raise ValueError("project manifest is invalid")
                latest = normalize_project(latest_raw, slug)
                valid, error = validate_project(latest, slug)
                if not valid:
                    raise ValueError(error or "project requires repair")

                assembly = latest.setdefault("assembly", {})
                # Rebuild only the primary lane. Existing item properties (trim,
                # speed, transforms, and transitions) survive a reorder; a
                # repeated clip receives a deterministic occurrence-specific ID.
                timeline = normalize_timeline(latest, assembly)
                main = next((track for track in timeline.get("tracks", [])
                             if isinstance(track, dict) and track.get("id") == "video-main"), None)
                if not isinstance(main, dict):
                    raise ValueError("timeline primary track unavailable")
                existing_by_clip: dict[str, list[dict]] = {}
                for item in main.get("clips", []) if isinstance(main.get("clips"), list) else []:
                    if isinstance(item, dict) and item.get("clip_id"):
                        existing_by_clip.setdefault(str(item["clip_id"]), []).append(item)
                rebuilt: list[dict] = []
                cursor = 0.0
                occurrences: dict[str, int] = {}
                for index, clip_id in enumerate(normalized_ids):
                    clip = _clip_lookup(latest).get(clip_id)
                    if not isinstance(clip, dict):
                        raise ValueError("unknown clip in assembly order")
                    occurrence = occurrences.get(clip_id, 0)
                    occurrences[clip_id] = occurrence + 1
                    candidates = existing_by_clip.get(clip_id, [])
                    old = candidates[occurrence] if occurrence < len(candidates) else None
                    if old is not None:
                        item = copy.deepcopy(old)
                        # A legacy timeline may have retained a stale version;
                        # normalize it against the current append-only clip
                        # record and fail closed if that version disappeared.
                        item = _timeline_item(item, clip_lookup=_clip_lookup(latest),
                                              track_id="video-main", index=index,
                                              document=latest)
                    else:
                        item = _timeline_item(
                            {"clip_id": clip_id, "start": cursor},
                            clip_lookup=_clip_lookup(latest), track_id="video-main", index=index,
                            document=latest,
                        )
                    if item is None:
                        raise ValueError("unable to materialize assembly timeline")
                    if occurrence:
                        item["id"] = stable_id("tl", "video-main", clip_id, item.get("version") or "latest", occurrence)
                    item["start"] = round(cursor, 6)
                    rebuilt.append(item)
                    cursor += max(0.001, _number(item.get("duration"), 0.001))
                main["clips"] = rebuilt
                _clamp_timeline_transitions(timeline)
                valid, error = validate_timeline(latest, timeline)
                if not valid:
                    raise ValueError(error or "invalid assembly timeline")

                assembly["timeline"] = timeline
                assembly["order"] = list(normalized_ids)
                finals = latest.setdefault("asset", {}).setdefault("finals", [])
                for old in finals:
                    if isinstance(old, dict) and old.get("kind") == "preview":
                        old["status"] = "superseded"
                finals.append({
                    "id": final_id, "kind": "preview", "file": rel,
                    "name": "Assembly preview", "preset": None,
                    "from": list(normalized_ids), "created": now(), "status": "active",
                })
                latest["asset"]["finals"] = finals
                latest["assets"] = latest["asset"].get("media", [])
                latest["clips"] = latest["asset"].get("clips", [])
                assembly["preview"] = rel
                current_rev = self._revision(latest.get("rev", 0))
                if current_rev is None:
                    current_rev = 0
                latest["rev"] = current_rev + 1
                latest["updated"] = now()
                valid, error = validate_project(latest, slug)
                if not valid:
                    raise ValueError(error or "invalid project")
                write_json_atomic(project_file(slug), latest)
                materialized_rev = latest["rev"]
                materialized = True
                # The manifest and generated file are the durable result.  A
                # best-effort log/index refresh must not turn a committed
                # preview into a dangling manifest reference if the process is
                # interrupted while maintaining a projection.
                try:
                    append_ops(slug, [{
                        "op": "assembly.preview_built", "order": list(normalized_ids),
                        "file": rel,
                    }], latest["rev"])
                except OSError:
                    pass
        except (OSError, ValueError, TypeError, KeyError):
            materialized = False

        if not materialized:
            # Do not leave an unindexed generated file behind when the project
            # disappears or becomes invalid during the compatibility operation.
            try:
                if os.path.isfile(output):
                    os.unlink(output)
            except OSError:
                pass
            return self._json(409, {"error": "assembly result could not be registered"})
        try:
            sync_index()
        except OSError:
            # ``index.json`` is a rebuildable projection; the next list read or
            # explicit sync will refresh it without invalidating this preview.
            pass
        return self._json(200, {
            "ok": True, "preview": rel, "final": final_id,
            "rev": materialized_rev,
        })

    def _trim(self, slug: str, query: dict[str, list[str]]) -> None:
        if not has_ffmpeg():
            return self._json(501, {"error": "ffmpeg unavailable"})
        payload = self._body_json(1 * 1024 * 1024)
        if not isinstance(payload, dict):
            return self._json(400, {"error": "bad request"})
        try:
            clip_id = str(payload["clip"])
            start, end = float(payload["in"]), float(payload["out"])
            if start < 0 or end <= start:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return self._json(400, {"error": "bad trim range"})
        document = normalize_project(read_json(project_file(slug), {}), slug)
        source = safe_file(slug, clip_media(document, clip_id, False))
        if not source:
            return self._json(400, {"error": "clip media is unavailable"})
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", clip_id)
        output = os.path.join(project_dir(slug), "clips", safe_id, f"trim-{time.time_ns()}.mp4")
        if not safe_write_target(project_dir(slug), output):
            return self._json(400, {"error": "invalid trim output path"})
        os.makedirs(os.path.dirname(output), exist_ok=True)
        try:
            result = ffmpeg_run(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", source,
                                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", output], 600)
        except (OSError, subprocess.TimeoutExpired):
            return self._json(500, {"error": "trim timed out"})
        if result.returncode != 0:
            return self._json(500, {"error": "trim failed"})
        rel = os.path.relpath(output, project_dir(slug)).replace(os.sep, "/")
        poster = os.path.join(project_dir(slug), "media", "posters", f"{safe_id}-{time.time_ns()}.jpg")
        poster_rel = None
        if safe_write_target(project_dir(slug), poster) and make_poster(output, poster):
            poster_rel = os.path.relpath(poster, project_dir(slug)).replace(os.sep, "/")
        return self._json(200, {"ok": True, "file": rel, "poster": poster_rel})

    def _adopt_first_video(self, slug: str) -> None:
        """First successful generation adopts its size, rate and timeline start."""

        payload = self._body_json(512 * 1024)
        if not isinstance(payload, dict):
            return self._json(400, {"error": "bad request"})
        asset_id = str(payload.get("asset_id") or "").strip()
        media_name = str(payload.get("name") or "").strip()[:160] or None
        rel = norm_rel(payload.get("file"))
        if not asset_id or not rel:
            return self._json(400, {"error": "asset_id and file are required"})
        try:
            width = int(payload.get("width") or 0)
            height = int(payload.get("height") or 0)
            fps = int(payload.get("fps") or 0)
            duration = max(0.1, min(24 * 60 * 60, float(payload.get("duration") or 0) or 1.0))
        except (TypeError, ValueError, OverflowError):
            return self._json(400, {"error": "bad media facts"})
        with LOCK:
            raw = read_json(project_file(slug), None)
            if not isinstance(raw, dict):
                return self._json(409, {"error": "project manifest is invalid"})
            document = normalize_project(raw, slug)
            plan = first_video_plan(document, width, height, fps)
            if not plan.get("adopt"):
                return self._json(200, {"ok": True, "adopted": False})
            assembly = document.setdefault("assembly", {})
            timeline = assembly.get("timeline")
            if not isinstance(timeline, dict):
                # A freshly created project has no timeline until the editor first
                # saves one, so build the schema default here.
                timeline = {
                    "canvas": dict(document.get("presets") or {}),
                    "tracks": [
                        {"id": "text-main", "kind": "subtitle", "muted": False, "hidden": False, "clips": [], "cues": []},
                        {"id": "video-main", "kind": "video", "muted": False, "hidden": False, "clips": []},
                        {"id": "video-overlay", "kind": "video", "muted": False, "hidden": False, "clips": []},
                        {"id": "video-2", "kind": "video", "muted": False, "hidden": False, "clips": []},
                        {"id": "video-3", "kind": "video", "muted": False, "hidden": False, "clips": []},
                        {"id": "audio-main", "kind": "audio", "muted": False, "hidden": False, "clips": []},
                    ],
                }
                assembly["timeline"] = timeline
            track = next((item for item in (timeline.get("tracks") or [])
                          if isinstance(item, dict) and item.get("id") == "video-main"), None)
            if track is None:
                return self._json(409, {"error": "video-main track is missing"})
            if plan.get("canvas"):
                timeline["canvas"] = {**(timeline.get("canvas") or {}), **plan["canvas"]}
                document.setdefault("presets", {})["ratio"] = plan["canvas"]["ratio"]
            if plan.get("fps"):
                timeline["fps"] = plan["fps"]
            clip_id = apply_first_video_adoption(slug, document, asset_id, rel, width, height,
                                                 fps, duration, media_name)
            if not clip_id:
                return self._json(409, {"error": "clip record could not be created"})
            append_ops(slug, [{"op": "timeline.adopt_first_video", "item_id": clip_id,
                               "asset_id": asset_id}], document["rev"])
            write_json_atomic(project_file(slug), document)
            clip = {"id": clip_id}
        sync_index()
        return self._json(200, {"ok": True, "adopted": True, "clip": clip["id"]})

    def _export(self, slug: str, query: dict[str, list[str]]) -> None:
        if not has_ffmpeg():
            return self._json(501, {"error": "ffmpeg unavailable"})
        payload = self._body_json(1 * 1024 * 1024)
        preset = payload.get("preset") if isinstance(payload, dict) else {}
        if not isinstance(preset, dict):
            preset = {}
        try:
            label = re.sub(r"[^\w\-一-鿿]+", "_", str(preset.get("label") or "export"))[:40] or "export"
            width, height = int(preset.get("w") or 1080), int(preset.get("h") or 1920)
        except (TypeError, ValueError):
            return self._json(400, {"error": "bad preset"})
        if not (64 <= width <= 4096 and 64 <= height <= 4096):
            return self._json(400, {"error": "bad preset"})
        quality = normalize_quality(preset.get("quality"))
        document = normalize_project(read_json(project_file(slug), {}), slug)
        order = payload.get("ids") if isinstance(payload, dict) else None
        if not isinstance(order, list):
            order = document.get("assembly", {}).get("order") or []
        order = [str(item) for item in order]
        if not order:
            return self._json(400, {"error": "assembly order is empty"})
        sources = []
        for clip_id in order:
            source = safe_file(slug, clip_media(document, str(clip_id), False))
            if not source:
                return self._json(400, {"error": "clip media is unavailable"})
            sources.append(source)
        output = os.path.join(project_dir(slug), "output", "exports", f"{label}-{time.time_ns()}.mp4")
        if not safe_write_target(project_dir(slug), output):
            return self._json(400, {"error": "invalid export output path"})
        fps = int(document.get("assembly", {}).get("timeline", {}).get("fps") or 30)
        ok, message = concat_reencode(sources, output, width=width, height=height, crf=20, fps=fps,
                                      bitrate_kbps=export_bitrate_kbps(width, height, fps, quality))
        if not ok:
            return self._json(500, {"error": message})
        return self._json(200, {"ok": True, "file": os.path.relpath(output, project_dir(slug)).replace(os.sep, "/"),
                               "preset": preset.get("label") or "export"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=default_root())
    parser.add_argument("--port", type=int, default=int(os.environ.get("VAM_PORT", os.environ.get("VPM_PORT", "4200"))))
    parser.add_argument("--bind", default=default_bind())
    parser.add_argument(
        "--preview-base",
        default="/",
        help="legacy compatibility argument; only / is accepted",
    )
    parser.add_argument("--task-dir", default=(os.environ.get("VIDEO_GENERATOR_HANDOFF_DIR")
                                                or os.environ.get("VIDEO_GENERATOR_TASK_DIR")
                                                or os.environ.get("VAM_HANDOFF_DIR")),
                        help="optional video-generator handoff inbox for live scans")
    parser.add_argument("--map", dest="task_map", default=None,
                        help="optional task-to-project mapping JSON for live scans")
    args = parser.parse_args()
    global ROOT, TASK_DIR, TASK_MAP, PREVIEW_BASE
    ROOT = os.path.abspath(os.path.expanduser(args.root))
    TASK_DIR = os.path.abspath(os.path.expanduser(args.task_dir)) if args.task_dir else None
    TASK_MAP = os.path.abspath(os.path.expanduser(args.task_map)) if args.task_map else None
    try:
        PREVIEW_BASE = normalize_preview_base(args.preview_base)
    except ValueError:
        raise SystemExit("invalid --preview-base; expected /")
    os.makedirs(ROOT, exist_ok=True)
    ensure_embedded_webapp()
    # Remove leftover render artifacts from previous crashed/restarted jobs.
    try:
        _sweep_orphan_render_artifacts()
    except Exception:
        pass
    sync_index()
    schedule_task_sync(force=True)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    start_task_watcher()
    print(f"video-asset-manager listening on {args.bind}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_task_watcher()
        server.server_close()


if __name__ == "__main__":
    main()
