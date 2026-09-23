#!/usr/bin/env python3
"""Generic video-generator completion bridge.

This is the only producer-facing helper in ``video-asset-manager``.  A video
skill (or its host/orchestrator) passes a public terminal result to this
command; the helper writes one atomic handoff document, starts/probes the
manager, and performs an idempotent receive.  It never calls a video
provider, reads credentials, or copies the source result JSON into a project.

Examples::

    python receive.py --result result.json
    type result.json | python receive.py --result -
    python receive.py --video C:\\outputs\\clip.webm --script "公开视频脚本" --title "Product demo"

The result may contain a local video path or a controlled HTTPS/S3 reference.
The handoff adapter performs the final path, remote-cache, privacy, and
project-registration checks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve()
SCRIPT_DIR = HERE.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from vpm_sync import (  # type: ignore
        DEFAULT_BIND,
        DEFAULT_PORT,
        resolve_root,
        resolve_task_dir,
        ensure_runtime,
        scan,
    )
except Exception as exc:  # pragma: no cover - only reached by a broken install
    raise SystemExit(f"video-asset-manager 接收入口不可用：{exc}") from exc

# ``vpm_receive`` contains the bounded file/directory staging implementation.
# Keep this short-name entrypoint as the public completion hook while reusing
# that implementation for local files (so an arbitrary absolute output path is
# copied into a safe temporary handoff before the scanner validates it).
try:
    import vpm_receive as _file_receiver  # type: ignore
except Exception:  # pragma: no cover - a source-only minimal install
    _file_receiver = None
try:
    import handoff_core as handoff_module  # type: ignore
except Exception:  # pragma: no cover - compatibility with minimal bundles
    import handoff_impl as handoff_module  # type: ignore


VIDEO_EXTS = {
    ".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi", ".mpeg", ".mpg",
    ".ts", ".ogv", ".3gp", ".flv",
}
TERMINAL_STATUSES = {
    "complete", "completed", "success", "succeeded", "done", "failed",
    "error", "cancelled", "canceled", "timed_out", "timeout", "partial",
}
PRIVATE_RE = re.compile(
    r"(?:api[_-]?key|apikey|secret|password|authorization|bearer\s|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|signed[_-]?url|"
    r"provider|model[_-]?route|stack\s*trace)", re.I,
)
ABSOLUTE_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|users|tmp|var|private|root)(?:/|$))",
    re.I,
)
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
CLIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
REMOTE_RE = re.compile(r"^(?:https?://|s3://)[^\s]+$", re.I)
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024

# Only these keys are allowed to cross the generator/manager boundary.  This
# deliberately excludes arbitrary nested provider payloads and prompts.
PUBLIC_KEYS = {
    "task_id", "id", "status", "task_status", "generator", "generator_name",
    "source", "producer", "project_slug", "manager_project", "video_project",
    "capafy_project_slug", "project", "clip_id", "manager_clip_id", "clip",
    "segment_id", "manager_task_id", "preflight_task_id", "prepared_task_id",
    "manager_request_id", "request_id", "video_file", "videoFile", "video_path", "videoPath",
    "local_video", "output_file", "local_output", "result_file", "artifact_file",
    "media_file", "video_output", "video_url", "videoUrl", "download_url",
    "downloadUrl", "file_url", "fileUrl", "object_url", "objectUrl", "s3_url",
    "s3Url", "video_uri", "videoUri", "resource_url", "resourceUrl",
    "download_link", "downloadLink", "object_key", "objectKey", "file_key",
    "fileKey", "resource_key", "resourceKey", "report", "report_file",
    "analysis_report", "project_title", "title", "name", "summary",
    "user_summary", "message", "duration", "ratio", "clarity", "outputs",
    "input_script", "public_script", "public_input", "input_script_file",
    "public_script_file", "input_script_role", "public_script_role",
    "input_script_name", "public_script_name", "input_script_format",
    "public_script_format",
}
VIDEO_KEYS = (
    "video_file", "videoFile", "video_path", "videoPath", "local_video",
    "output_file", "local_output", "result_file", "artifact_file", "media_file",
    "video_output", "video_url", "videoUrl", "download_url", "downloadUrl",
    "file_url", "fileUrl", "object_url", "objectUrl", "s3_url", "s3Url",
    "video_uri", "videoUri", "resource_url", "resourceUrl", "download_link",
    "downloadLink", "object_key", "objectKey", "file_key", "fileKey",
    "resource_key", "resourceKey",
)
VIDEO_RESULT_CONTAINER_KEYS = {
    "task", "data", "response", "payload", "content", "delivery",
    "artifact", "artifacts", "media",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clean_text(value: Any, limit: int = 500, fallback: str | None = None) -> str | None:
    if value is None:
        return fallback
    text = str(value).replace("\x00", "").strip()
    if not text or len(text) > limit or PRIVATE_RE.search(text) or ABSOLUTE_RE.search(text):
        return fallback
    if re.search(r"(?:https?://|data:)", text, re.I):
        return fallback
    return text


def _safe_ref(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").strip()
    if not text or len(text) > 16_384 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return None
    if REMOTE_RE.fullmatch(text):
        # Reject credentials embedded in a URL.  The handoff adapter applies
        # the stricter host/redirect policy when it caches the object.
        try:
            from urllib.parse import urlparse
            parsed = urlparse(text)
            if parsed.username is not None or parsed.password is not None or not parsed.netloc:
                return None
        except ValueError:
            return None
        return text
    # Local paths are intentionally accepted here; they are revalidated by
    # approved_local_file() against configured output roots during handoff.
    return text


def _looks_like_video(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.split("?", 1)[0].split("#", 1)[0].lower().replace("\\", "/")
    return Path(text).suffix in VIDEO_EXTS or REMOTE_RE.fullmatch(value.strip()) is not None


def _iter_video_values(value: Any, *, depth: int = 0,
                       result_context: bool = False) -> Iterable[str]:
    """Yield video references from common generator result envelopes.

    Generator APIs do not agree on one response shape.  In particular, many
    return ``{"result": {"url": ...}}`` or ``{"outputs": [{"href": ...}]}``
    instead of the explicit ``video_url``/``video_file`` aliases.  We only
    inspect generic URL/path keys after crossing an output/result/media
    boundary; top-level ``url`` values remain ignored so a report or callback
    link can never be mistaken for a video.  The depth and list caps keep this
    discovery bounded just like the explicit-key path.
    """
    if depth > 8:
        return
    if isinstance(value, str):
        if _looks_like_video(value):
            yield value
        return
    if isinstance(value, list):
        for child in value[:100]:
            yield from _iter_video_values(
                child, depth=depth + 1, result_context=result_context,
            )
        return
    if not isinstance(value, dict):
        return
    for key in VIDEO_KEYS:
        if key in value:
            yield from _iter_video_values(
                value.get(key), depth=depth + 1, result_context=True,
            )
    # A common generic result shape is {outputs: [{file: ...}]}; only inspect
    # output-like keys, never prompts, diagnostics, or arbitrary text.
    generic_result_keys = {"url", "href", "uri", "path", "location", "key"}
    for key, child in value.items():
        name = str(key).lower().replace("-", "_")
        boundary = any(token in name for token in (
            "video", "download", "file", "object", "media", "output", "result",
        ))
        if boundary or name in VIDEO_RESULT_CONTAINER_KEYS:
            yield from _iter_video_values(
                child, depth=depth + 1, result_context=True,
            )
        elif result_context and name in generic_result_keys:
            # ``url``/``href`` are intentionally accepted only inside a
            # result-like object, never from an arbitrary top-level payload.
            yield from _iter_video_values(
                child, depth=depth + 1, result_context=True,
            )


def _first_video(payload: dict[str, Any]) -> str | None:
    for value in _iter_video_values(payload):
        ref = _safe_ref(value)
        if ref:
            return ref
    return None


def _field(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def _safe_identifier(value: Any, pattern: re.Pattern[str], prefix: str, *parts: Any) -> str:
    text = str(value or "").strip()
    if pattern.fullmatch(text) and not PRIVATE_RE.search(text):
        return text
    raw = "\x1f".join(str(part or "") for part in (prefix, *parts, text))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _safe_slug(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if SLUG_RE.fullmatch(text) and not PRIVATE_RE.search(text) else None


def _safe_clip(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if CLIP_RE.fullmatch(text) and not PRIVATE_RE.search(text) else None


def _pick_mapping(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    projects: list[str] = []
    for key in ("project_slug", "manager_project", "video_project", "capafy_project_slug", "project"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            raw = raw.get("slug", raw.get("project_slug", raw.get("id")))
        slug = _safe_slug(raw)
        if raw is not None and slug:
            projects.append(slug)
    project = projects[0] if projects and len(set(projects)) == 1 else None
    clips: list[str] = []
    for key in ("clip_id", "manager_clip_id", "clip", "segment_id"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            raw = raw.get("id", raw.get("clip_id"))
        clip = _safe_clip(raw)
        if raw is not None and clip:
            clips.append(clip)
    clip = clips[0] if clips and len(set(clips)) == 1 else None
    return project, clip


def _parse_json_text(raw: str) -> dict[str, Any]:
    """Parse one bounded JSON object from an inline value or stdin."""
    if len(raw.encode("utf-8", "ignore")) > MAX_JSON_BYTES:
        raise ValueError("结果文件不可用。")
    try:
        value = json.loads(raw or "{}")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("结果文件不是有效 JSON。") from exc
    if not isinstance(value, dict):
        raise ValueError("结果必须是 JSON 对象。")
    return value


def _load_result(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    if path == "-":
        return _parse_json_text(sys.stdin.read())
    result_path = Path(path).expanduser()
    try:
        if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("结果文件不可用。")
        raw = result_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("结果文件不可用。") from exc
    return _parse_json_text(raw)


def _looks_like_json_inline(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    text = value.lstrip()
    return text.startswith("{") or text.startswith("[")


def _inline_public_paths(source: dict[str, Any]) -> dict[str, Any]:
    """Resolve existing relative media/report references against cwd.

    Inline JSON and stdin have no source-file directory.  Resolving only
    already-existing, non-symlink files keeps the handoff useful without
    turning arbitrary strings into absolute paths or broadening trust roots.
    """
    result = dict(source)
    path_keys = {
        "video_file", "videoFile", "video_path", "videoPath", "local_video",
        "output_file", "local_output", "result_file", "artifact_file",
        "media_file", "video_output", "report_file", "analysis_report",
    }
    for key in path_keys:
        value = result.get(key)
        if not isinstance(value, str) or not value.strip() or REMOTE_RE.fullmatch(value.strip()):
            continue
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            continue
        candidate = Path.cwd() / candidate
        try:
            if candidate.is_file() and not candidate.is_symlink():
                result[key] = str(candidate.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    return result


def _write_temporary_result(source: dict[str, Any]) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory(prefix="vam-result-")
    path = Path(temporary.name) / "result.json"
    path.write_text(
        json.dumps(_inline_public_paths(source), ensure_ascii=False),
        encoding="utf-8",
    )
    return path, temporary


def _source_path_from_args(args: argparse.Namespace) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    """Resolve result-file/inline JSON/stdin/positional input to one path.

    The returned temporary file contains only the eventual public JSON input;
    the receiver performs the final allow-list and privacy pass before it is
    staged into the manager inbox.
    """
    result_value = getattr(args, "result", None)
    json_value = getattr(args, "json_input", None)
    positional = getattr(args, "input", None)
    selectors = [item for item in (result_value, json_value, positional) if item is not None]
    if len(selectors) > 1:
        raise ValueError("结果输入只能指定一次。")

    if json_value is not None:
        # ``--json`` accepts an inline object, a JSON file path, or ``-`` for
        # stdin.  A path is preferred only when it is an existing regular
        # file; otherwise the value is parsed as JSON text.
        if json_value == "-":
            source = _load_result("-")
        elif _looks_like_json_inline(json_value):
            source = _parse_json_text(json_value)
        else:
            candidate = Path(json_value).expanduser()
            if candidate.is_file() and not candidate.is_symlink():
                # Keep the original file as the source so relative
                # ``video_file``/``report_file`` references continue to be
                # resolved beside that result document.
                _load_result(str(candidate))
                return candidate, None
            else:
                source = _parse_json_text(json_value)
        return _write_temporary_result(source)

    selected = result_value if result_value is not None else positional
    if selected is not None:
        if selected == "-":
            source = _load_result("-")
            return _write_temporary_result(source)
        candidate = Path(selected).expanduser()
        try:
            if candidate.is_symlink() or not candidate.exists():
                raise ValueError("输入结果不存在。")
        except OSError as exc:
            raise ValueError("输入结果不可用。") from exc
        # JSON files are validated eagerly.  A video file or result directory
        # is passed through to vpm_receive, which knows how to discover it.
        if candidate.is_file() and candidate.suffix.lower() == ".json":
            _load_result(str(candidate))
        return candidate, None

    # An explicit --video/--video-url is a complete selector: the inbox
    # record is built from the CLI flags alone.  Short-circuit BEFORE the
    # stdin probe (a pipe with no explicit selector is the only stdin form,
    # and test runners substitute a guard object that raises on read).
    if getattr(args, "video", None) or getattr(args, "video_url", None):
        return _write_temporary_result({})

    # A pipe with no explicit selector is a supported stdin form.  Do not
    # read an interactive terminal, where this would otherwise block forever.
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            source = _parse_json_text(raw)
            return _write_temporary_result(source)

    raise ValueError("请提供结果 JSON、视频文件、结果目录或 stdin。")


def _public_payload(source: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Build a minimal, generator-neutral inbox record."""
    payload: dict[str, Any] = {}
    script_keys = {
        "input_script", "public_script", "public_input", "input_script_file",
        "public_script_file", "input_script_role", "public_script_role",
        "input_script_name", "public_script_name", "input_script_format",
        "public_script_format",
    }
    for key in PUBLIC_KEYS:
        if key in source:
            value = source.get(key)
            if key in script_keys:
                # Script fields are handled below through the dedicated public
                # sanitizer; never copy a raw prompt/brief value wholesale.
                continue
            if key == "outputs":
                # Keep only a small list of scalar output references; nested
                # provider metadata is deliberately dropped.
                if isinstance(value, list):
                    clean_outputs: list[Any] = []
                    for item in value[:20]:
                        if isinstance(item, str):
                            ref = _safe_ref(item)
                            if ref:
                                clean_outputs.append(ref)
                        elif isinstance(item, dict):
                            ref = _first_video(item)
                            if ref:
                                clean_outputs.append({"file": ref})
                    if clean_outputs:
                        payload[key] = clean_outputs
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value

    script_value = None
    for key in (
        "input_script", "public_script", "public_input",
    ):
        if source.get(key) is not None:
            script_value = source.get(key)
            break
    if script_value is not None:
        try:
            info = handoff_module.public_script_info(
                script_value,
                name=next((source.get(k) for k in ("input_script_name", "public_script_name") if source.get(k) is not None), None),
                role=next((source.get(k) for k in ("input_script_role", "public_script_role") if source.get(k) is not None), None),
                fmt=next((source.get(k) for k in ("input_script_format", "public_script_format") if source.get(k) is not None), None),
            )
        except Exception:
            info = None
        if info:
            payload["input_script"] = info["text"]
            payload["input_script_role"] = info["role"]
            payload["input_script_name"] = info["name"]
            payload["input_script_format"] = info["format"]

    raw_video = getattr(args, "video", None)
    remote_video = (
        isinstance(raw_video, str)
        and re.match(r"^(?:https?|s3)://", raw_video.strip(), re.I)
    )
    overrides = {
        "task_id": args.task_id,
        "generator": args.generator,
        "project_title": args.title,
        "project_slug": args.project,
        "clip_id": args.clip,
        "video_file": None if remote_video else raw_video,
        "video_url": getattr(args, "video_url", None) or (raw_video if remote_video else None),
        "report": args.report,
        "input_script": getattr(args, "script", None),
        "input_script_file": getattr(args, "script_file", None),
        "input_script_role": getattr(args, "script_role", None),
        "input_script_name": getattr(args, "script_name", None),
        "input_script_format": getattr(args, "script_format", None),
        "status": args.status,
        "message": args.message,
    }
    for key, value in overrides.items():
        if value is not None:
            payload[key] = value

    video = _first_video(payload)
    if video and not any(payload.get(key) for key in VIDEO_KEYS):
        payload["video_file"] = video
    if not payload.get("status"):
        payload["status"] = "completed" if _first_video(payload) else "partial"
    status = str(payload.get("status") or "").strip().lower()
    if status not in TERMINAL_STATUSES:
        # Do not turn an in-flight provider response into a completed project.
        payload["status"] = "partial"
    if not payload.get("generator"):
        payload["generator"] = "video-generator"
    payload["generator"] = re.sub(r"[^A-Za-z0-9._-]+", "-", str(payload["generator"]))[:40].strip("-._") or "video-generator"

    # Keep manager-preflight aliases as safe scalar identifiers for the
    # bundled scanner.  The minimal-install fallback does not run
    # vpm_receive's richer sanitizer, so validate these explicitly here and
    # drop malformed/private values rather than persisting arbitrary text.
    for key in (
        "manager_task_id", "preflight_task_id", "prepared_task_id",
        "manager_request_id", "request_id",
    ):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if TASK_RE.fullmatch(text) and not PRIVATE_RE.search(text):
            payload[key] = text
        else:
            payload.pop(key, None)
    project, clip = _pick_mapping(payload)
    if project:
        payload["project_slug"] = project
    else:
        payload.pop("project_slug", None)
    if clip:
        payload["clip_id"] = clip
    else:
        payload.pop("clip_id", None)
    title = _clean_text(_field(payload, "project_title", "title", "name", "summary", "message"), 80)
    if title:
        payload["project_title"] = title
    else:
        payload.pop("project_title", None)
    message = _clean_text(_field(payload, "message", "summary", "user_summary"), 500)
    if message:
        payload["message"] = message
    else:
        payload.pop("message", None)
    # Preserve only approved scalar metadata.  This field lets the receiver
    # distinguish a fresh handoff from an in-flight file without exposing
    # provider internals.
    payload["received_at"] = _now()
    if not payload.get("task_id"):
        identity = json.dumps({k: payload.get(k) for k in sorted(payload) if k != "received_at"}, ensure_ascii=False, sort_keys=True)
        payload["task_id"] = _safe_identifier(None, TASK_RE, "task", identity)
    else:
        payload["task_id"] = _safe_identifier(payload.get("task_id"), TASK_RE, "task", payload.get("generator"), payload.get("video_file"), payload.get("video_url"))
    # An inbox record may contain a local path transiently so the adapter can
    # validate it.  It is never copied into the project manifest or logs.
    return payload


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError("接收目录中的目标文件不可用。")
    fd, temporary = tempfile.mkstemp(prefix=".handoff.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _task_dir(root: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve(strict=False)
    else:
        # Respect the shared handoff environment when configured.  Otherwise
        # keep the default inbox inside the manager workspace.
        path = resolve_task_dir(None)
        if path == Path(tempfile.gettempdir()) / "video-generator-handoffs":
            path = root / "inbox"
        else:
            path = Path(path)
    if path.exists() and path.is_symlink():
        raise ValueError("接收目录不可用。")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("接收目录不可用。")
    return path.resolve(strict=False)


def _safe_runtime_summary(runtime: object) -> dict[str, Any]:
    """Keep only the current manager runtime/window contract.

    The manager owns a plain HTTP window on its fixed port.  Historical
    instance-scoped Preview fields are intentionally not carried forward:
    accepting them here made an old envelope look like a current launch.
    """

    if not isinstance(runtime, dict):
        return {}
    result: dict[str, Any] = {
        key: runtime[key]
        for key in ("ok", "started_now", "mode", "port", "health", "entry")
        if key in runtime and isinstance(runtime[key], (bool, int, float, str))
    }
    current_runtime = result.get("mode") == "managed-http"
    if not current_runtime:
        result.pop("mode", None)
    window = runtime.get("window")
    if isinstance(window, dict):
        safe_window = {
            key: window[key]
            for key in ("open", "action", "transport", "port", "route")
            if key in window and isinstance(window[key], (bool, int, float, str))
        }
        if safe_window:
            result["window"] = safe_window
    # Read-only compatibility for historical handoff summaries.  Current
    # managed-http responses never carry this field; only an older runtime
    # envelope with a validated instance-relative path may be echoed.
    if not current_runtime:
        preview = runtime.get("preview")
        if isinstance(preview, dict):
            path = preview.get("path")
            if isinstance(path, str) and re.fullmatch(r"/instance/[^/\\\r\n]+/", path):
                safe_preview = {
                    key: preview[key]
                    for key in ("status", "version", "path")
                    if key in preview and isinstance(preview[key], (bool, int, float, str))
                }
                if safe_preview:
                    result["preview"] = safe_preview
    return result


def _result_summary(
    scan_result: dict[str, Any],
    handoff_path: Path,
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": bool(scan_result.get("ok", False)),
        "scanned": int(scan_result.get("scanned", scan_result.get("eligible", 0)) or 0),
        "imported": int(scan_result.get("imported", 0) or 0),
        "idempotent": int(scan_result.get("idempotent", 0) or 0),
        "pending": int(scan_result.get("pending_count", 0) or 0),
        "handoff_written": True,
        "handoff_name": handoff_path.name,
    }
    # A manager runtime/Preview outage is orthogonal to delivery. Preserve a
    # short warning for the host while keeping ``ok`` tied to the filesystem
    # handoff result so a generated video is never reported as failed solely
    # because its window could not be opened.
    warning = scan_result.get("manager_warning")
    if isinstance(warning, str) and warning.strip():
        result["manager_warning"] = warning[:240]
    runtime_summary = _safe_runtime_summary(scan_result.get("runtime"))
    if runtime_summary:
        result["runtime"] = runtime_summary
    received = scan_result.get("received")
    if isinstance(received, list) and received:
        first = {}
        if task_id:
            first = next(
                (item for item in received
                 if isinstance(item, dict) and item.get("task_id") == task_id),
                {},
            )
        if not first:
            first = received[0] if isinstance(received[0], dict) else {}
        for key in ("task_id", "project", "clip_id", "status", "idempotent",
                    "report", "script", "message", "outputs"):
            if key in first:
                value = first[key]
                result[key] = value[:500] if isinstance(value, str) else value
    results = scan_result.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        first = results[0]
        for key in ("task_id", "project", "clip_id", "status", "idempotent",
                    "report", "script", "message", "outputs", "error", "reason"):
            if key in first and key not in result:
                value = first[key]
                result[key] = value[:500] if isinstance(value, str) else value
    return result


def _best_effort_runtime(root: Path, port: int, bind: str) -> dict[str, Any]:
    """Probe the manager without allowing a runtime outage to fail intake."""

    try:
        value = ensure_runtime(root, port, bind)
    except Exception:
        return {"ok": False, "error": "管理运行时未能启动。"}
    if not isinstance(value, dict):
        return {"ok": False, "error": "管理运行时未能启动。"}
    return value


def _port_arg(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("端口无效。") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口无效。")
    return port


def _public_error(exc: BaseException) -> str:
    """Map bridge failures to a short message without echoing paths/URLs."""
    if isinstance(exc, ValueError):
        text = str(exc)
        allowed = {
            "结果文件不可用。", "结果文件不是有效 JSON。", "结果必须是 JSON 对象。",
            "输入结果不存在。", "输入结果不可用。", "结果输入只能指定一次。",
            "请提供结果 JSON、视频文件、结果目录或 stdin。",
        }
        if text in allowed:
            return text
    return "视频结果接收失败。"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Receive a generic video-generator result into video-asset-manager")
    parser.add_argument("input", nargs="?", help="result JSON/video file/directory (legacy positional form)")
    parser.add_argument("--result", "--result-file", dest="result", default=None,
                        help="JSON result file, or - for stdin")
    parser.add_argument("--json", dest="json_input", nargs="?", const="-",
                        help="inline JSON object, JSON file path, or - for stdin")
    parser.add_argument("--video", "--video-file", dest="video",
                        help="local video path (or a remote HTTPS/S3 reference)")
    parser.add_argument("--video-url", dest="video_url",
                        help="remote HTTPS/S3 video reference")
    parser.add_argument("--report", help="public report path or text")
    parser.add_argument("--script", "--input-script", dest="script",
                        help="public original input script/brief text")
    parser.add_argument("--script-file", "--input-script-file", dest="script_file",
                        help="public original input script file")
    parser.add_argument("--script-role", choices=("script", "storyboard", "subtitle", "analysis_report"))
    parser.add_argument("--script-name")
    parser.add_argument("--script-format")
    parser.add_argument("--task-id")
    parser.add_argument("--generator")
    parser.add_argument("--title")
    parser.add_argument("--project", "--project-slug", dest="project")
    parser.add_argument("--clip", "--clip-id", dest="clip")
    parser.add_argument("--status")
    parser.add_argument("--message")
    parser.add_argument("--root")
    parser.add_argument("--task-dir")
    parser.add_argument("--port", type=_port_arg, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--no-open", action="store_true",
                        help="do not start runtime; sync only when it is already running")
    parser.add_argument("--no-sync", action="store_true",
                        help="write handoff and skip scanning; starts runtime unless --no-open")
    args = parser.parse_args(argv)

    temporary_result: tempfile.TemporaryDirectory[str] | None = None
    try:
        source_path, temporary_result = _source_path_from_args(args)
        root = resolve_root(args.root)
        task_dir = _task_dir(root, args.task_dir)

        # Inline JSON/stdin is written to a temporary directory, so the
        # handoff adapter cannot use that file's parent as an approved root
        # for an absolute ``video_file``.  Read the sanitized input once and
        # pass an explicit local override; vpm_receive then copies it into the
        # inbox and removes the absolute path from the persisted record.
        source_payload: dict[str, Any] = {}
        if source_path.is_file() and source_path.suffix.lower() == ".json":
            source_payload = _load_result(str(source_path))

        # Keep the two forms distinct internally: ``--video`` historically
        # accepted a remote reference, while the receiver's ``video`` argument
        # is intentionally local-only.  Route remote values through the
        # dedicated URL field so they can be cached and never become project
        # metadata.
        # Remember whether the local path came directly from the CLI.  A
        # detected path inside a result JSON must not inherit the explicit
        # path trust level merely because it is later assigned to the same
        # variable.
        video_cli_explicit = bool(args.video)
        video_override = args.video
        video_url_override = args.video_url
        if not video_override and not video_url_override and source_payload:
            detected_video = _first_video(source_payload)
            if detected_video:
                if REMOTE_RE.fullmatch(detected_video):
                    video_url_override = detected_video
                else:
                    video_override = detected_video
        if isinstance(video_override, str) and re.match(
            r"^(?:https?|s3)://", video_override.strip(), re.I
        ):
            video_url_override = video_url_override or video_override
            video_override = None

        common = dict(
            root=args.root,
            task_dir=str(task_dir),
            project=args.project,
            clip=args.clip,
            video=video_override,
            video_url=video_url_override,
            generator=args.generator,
            title=args.title,
            report=args.report,
            script=args.script,
            script_file=args.script_file,
            script_role=args.script_role,
            script_name=args.script_name,
            script_format=args.script_format,
            task_id=args.task_id,
            status=args.status,
            message=args.message,
            # ``--video`` is an explicit caller-selected local path. Values
            # discovered from a result JSON are still checked against the
            # handoff's approved roots by vpm_receive.
            video_external=video_cli_explicit and bool(video_override),
        )

        if _file_receiver is not None and hasattr(_file_receiver, "stage_handoff"):
            # Always stage through the generic receiver first.  This gives the
            # inbox an atomic, sanitized handoff for every mode and copies a
            # direct local video beside it instead of persisting an absolute
            # source path.
            handoff_path = _file_receiver.stage_handoff(source_path, **common)

            if args.no_sync:
                runtime: dict[str, Any] | None = None
                if not args.no_open:
                    runtime = _best_effort_runtime(root, args.port, str(args.bind or DEFAULT_BIND))
                result: dict[str, Any] = {
                    # The handoff has already been atomically staged. Runtime
                    # startup is best-effort and must not turn that durable
                    # delivery record into a failed generator result.
                    "ok": True,
                    "handoff_written": True,
                    "handoff_name": handoff_path.name,
                    "scanned": 0,
                }
                if runtime is not None:
                    result["runtime"] = _safe_runtime_summary(runtime)
                    if not runtime.get("ok", False):
                        result["manager_warning"] = "管理运行时未能启动。"
                print(json.dumps(result, ensure_ascii=False))
                return 0

            # ``--no-open`` explicitly forbids startup.  A scan can still
            # register into the filesystem and an already-open manager will
            # observe it through its watcher; no ensure call is made here.
            scan_result = scan(
                root,
                task_dir,
                project=_safe_slug(args.project),
                clip=_safe_clip(args.clip),
                start_runtime=not args.no_open,
                port=args.port,
                bind=str(args.bind or DEFAULT_BIND),
                max_tasks=100,
            )
            result = _result_summary(scan_result, handoff_path, task_id=handoff_path.stem)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("ok", False) else 1

        # Minimal-install fallback: keep the original allow-listed writer if
        # the bundled vpm_receive module is unavailable.
        source = _load_result(str(source_path)) if source_path.suffix.lower() == ".json" else {}
        payload = _public_payload(source, args)
        handoff_path = task_dir / f"{payload['task_id']}.json"
        _atomic_write(handoff_path, payload)
        if args.no_sync:
            runtime = (_best_effort_runtime(root, args.port, str(args.bind or DEFAULT_BIND))
                       if not args.no_open else None)
            result = {
                "ok": True,
                "handoff_written": True,
                "handoff_name": handoff_path.name,
                "scanned": 0,
            }
            if runtime is not None:
                result["runtime"] = _safe_runtime_summary(runtime)
                if not runtime.get("ok", False):
                    result["manager_warning"] = "管理运行时未能启动。"
            print(json.dumps(result, ensure_ascii=False))
            return 0
        scan_result = scan(
            root, task_dir, project=_safe_slug(payload.get("project_slug")),
            clip=_safe_clip(payload.get("clip_id")), start_runtime=not args.no_open,
            port=args.port, bind=str(args.bind or DEFAULT_BIND), max_tasks=100,
        )
        result = _result_summary(scan_result, handoff_path, task_id=handoff_path.stem)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok", False) else 1
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": _public_error(exc)}, ensure_ascii=False))
        return 1
    except Exception:
        # Keep the completion bridge customer-safe even when the delegated
        # receiver rejects a malformed result or an unavailable local file.
        print(json.dumps({"ok": False, "error": "视频结果接收失败。"}, ensure_ascii=False))
        return 1
    finally:
        if temporary_result is not None:
            temporary_result.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
